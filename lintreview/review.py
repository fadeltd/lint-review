from __future__ import absolute_import
from collections import OrderedDict
import logging

log = logging.getLogger(__name__)


class IssueLabel(object):

    def __init__(self, label):
        self.label = label

    def remove(self, pull_request):
        try:
            pull_request.remove_label(self.label)
        except Exception:
            log.warn("Failed to remove label '%s'", self.label)

    def publish(self, repo, pull_request):
        self.remove(pull_request)
        log.debug("Publishing issue label '%s'", self.label)
        try:
            repo.ensure_label(self.label)
            pull_request.add_label(self.label)
        except Exception:
            log.warn("Failed to add label '%s'", self.label)


class BaseComment(object):
    """Shared behavior across comment types
    """
    body = ''

    def key(self):
        """Define the identifying tuple for a comment.
        This should be a tuple of the file/position of the comment.
        """
        raise NotImplementedError()

    def __eq__(self, other):
        return False

    def append_body(self, text):
        if text not in self.body:
            self.body += "\n" + text

    def summary_text(self):
        return self.body


class IssueComment(BaseComment):
    """A simple comment that will be published as a
    pull request/issue comment.
    """
    def __init__(self, body=''):
        self.body = body

    def key(self):
        """IssueComments are unique based on their body
        """
        return (self.body, None)

    def __eq__(self, other):
        return self.body == other.body

    def __repr__(self):
        return u"{}(body={}".format(self.__class__.__name__,
                                    self.body)


class Comment(BaseComment):
    """A line comment on the pull request.

    The `line` attribute is populated when comments are built
    from tool output.

    The `line` attribute is then mapped into a `position` when
    a comment is merged with diff data.
    """
    line = 0
    position = 0
    body = ''
    filename = ''

    def __init__(self, filename='', line=0, position=0, body=''):
        self.body = body
        self.line = line
        self.filename = filename
        self.position = position

    def payload(self):
        return {
            'path': self.filename,
            'position': self.position,
            'body': self.body,
        }

    def key(self):
        return (self.filename, self.position)

    def summary_text(self):
        return u"{0.filename}, line {0.line} - {0.body}".format(self)

    def __eq__(self, other):
        return (self.filename == other.filename and
                self.position == other.position and
                self.body == other.body)

    def __repr__(self):
        return "%s(filename=%s, line=%s, position=%s, body=%s)" % (
            str(self.__class__.__name__),
            self.filename,
            self.line,
            self.position,
            self.body)


class Review(object):
    """Holds the comments from a review can
    add track problems logged and post new problems
    to github.
    """

    def __init__(self, repo, pull_request, config):
        self._repo = repo
        self._comments = Problems()
        self._pr = pull_request
        self.config = config

    def comments(self, filename):
        return self._comments.all(filename)

    def publish(self, problems, head_sha):
        """Publish the review.

        Existing comments are loaded, and compared
        to new problems. Once the new unique problems
        are distilled new comments are published.
        """
        if not problems.has_changes():
            return self.publish_empty_comment()

        self.load_comments()
        total_problem_count = len(problems)
        self.remove_existing(problems)

        new_problem_count = len(problems)

        threshold = self.config.summary_threshold()
        under_threshold = (threshold is None or
                           new_problem_count < threshold)

        if self.config.use_checks():
            return self.publish_checks(problems, head_sha)

        if under_threshold:
            self.publish_review(problems, head_sha)
        else:
            self.publish_summary(problems)
        self.publish_status(total_problem_count)

    def load_comments(self):
        """Load the existing comments on a pull request

        Results in a structure that is similar to the one used
        for problems
        """
        log.debug("Loading comments for pull request '%s'", self._pr.number)
        comments = list(self._pr.review_comments())

        for comment in comments:
            # Workaround github3 not exposing attributes for what we need.
            guts = comment.as_dict()
            filename = guts['path']
            if not guts['position']:
                log.debug("Ignoring outdated diff comment '%s'", comment.id)
                continue
            self._comments.add(
                filename,
                None,
                comment.body,
                int(guts['position']))
        log.debug("'%s' comments loaded", len(self._comments))

    def remove_existing(self, problems):
        """Modifies the problems parameter removing
        problems that already have matching comments.
        Filters the problems based on existing comments.

        Remove problems that match the line + comment body of
        an existing comment. We'll assume the program put
        the comment there, and not a human.
        """
        for comment in self._comments:
            problems.remove(comment)

    def publish_review(self, problems, head_commit):
        """Publish the issues contains in the problems
        parameter. changes is used to fetch the commit sha
        for the comments on a given file.
        """
        log.info("Publishing review of %s new comments for %s",
                 len(problems),
                 self._pr.display_name)
        self.remove_ok_label()
        review = self._build_review(problems, head_commit)
        if len(review['comments']) or len(review['body']):
            self._pr.create_review(review)

    def _build_review(self, problems, head_commit):
        """Because github3.py doesn't support creating reviews
        we use some workarounds
        """
        body = [
            comment.body
            for comment in problems
            if isinstance(comment, IssueComment)
        ]
        comments = [
            comment.payload()
            for comment in problems
            if isinstance(comment, Comment)
        ]
        review = {
            'commit_id': head_commit,
            'event': 'COMMENT',
            'body': "\n\n".join(body),
            'comments': comments
        }
        return review

    def publish_checkrun(self, problems, head_commit):
        """Publish the issues contained in the problems
        parameter. Changes is used to fetch the commit sha
        for the comments on a given file.
        """
        log.info("Publishing checkrun of %s new comments for %s",
                 len(problems),
                 self._pr.display_name)
        self.remove_ok_label()
        review = self._build_checkrun(problems, head_commit)
        if len(review['output']):
            self._pr.create_checkrun(review)

    def _build_checkrun(self, problems, head_commit):
        """Because github3.py doesn't support creating checkruns
        we use some workarounds.
        """
        pass

    def publish_status(self, problem_count):
        """Update the build status for the tip commit.
        The build will be a success if there are 0 problems.
        """
        state = self.config.failed_review_status()
        description = 'Lint errors found, see pull request comments.'
        if problem_count == 0:
            self.publish_ok_label()
            self.publish_ok_comment()
            state = 'success'
            description = 'No lint errors found.'
        self._repo.create_status(
            self._pr.head,
            state,
            description
        )

    def remove_ok_label(self):
        label = self.config.passed_review_label()
        if label:
            IssueLabel(label).remove(self._pr)

    def publish_ok_label(self):
        """Optionally publish the OK_LABEL if it is enabled.
        """
        label = self.config.passed_review_label()
        if label:
            issue_label = IssueLabel(label)
            issue_label.publish(self._repo, self._pr)

    def publish_ok_comment(self):
        """Optionally publish the OK_COMMENT if it is enabled.
        """
        comment = self.config.get('OK_COMMENT', False)
        if comment:
            self._pr.create_comment(comment)

    def publish_empty_comment(self):
        log.info('Publishing empty comment.')
        self.remove_ok_label()
        body = ('Could not review pull request. '
                'It may be too large, or contain no reviewable changes.')
        self._pr.create_comment(body)
        self._repo.create_status(
            self._pr.head,
            'success',
            body
        )

    def publish_summary(self, problems):
        num_comments = len(problems)
        log.info('Publishing summary comment for %s errors', num_comments)

        self.remove_ok_label()
        body = u"There are {0} errors:\n\n".format(num_comments)
        for problem in problems:
            body += u"* {}\n".format(problem.summary_text())
        self._pr.create_comment(body)


class Problems(object):
    """Collection class for holding all the problems found
    during automated review.

    Used by tool objects to collect problems, and by
    the Review objects to publish results.
    """
    def __init__(self, changes=None):
        self._items = OrderedDict()
        self._changes = changes

    def set_changes(self, changes):
        self._changes = changes

    def has_changes(self):
        return self._changes and len(self._changes) > 0

    def line_to_position(self, filename, line):
        """Convert the line number in the final file to a diff offset

        Saving comments in github requires line offsets no line numbers.
        Mapping line numbers makes saving possible.
        """
        if not self._changes:
            return line
        return self._changes.line_position(filename, line)

    def all(self, filename=None):
        if filename:
            return [error
                    for error in self
                    if error.filename == filename]
        return list(self._items.values())

    def add(self, filename, line=None, body=None, position=None):
        """Add a problem to the review.

        If position is not supplied the diff collection will be scanned
        and the line numbers diff offset will be fetched from there.
        """
        if isinstance(filename, BaseComment):
            self._items[filename.key()] = filename
            return

        if not position:
            position = self.line_to_position(filename, line)

        error = Comment(
            filename=filename,
            line=line,
            position=position,
            body=body)
        key = error.key()
        if key not in self._items:
            log.debug("Adding new line comment '%s'", error)
            self._items[key] = error
        else:
            log.debug("Updating existing line comment with '%s'", error)
            self._items[key].append_body(error.body)

    def add_many(self, problems):
        """Add multiple problems to the review.
        """
        for p in problems:
            self.add(p)

    def limit_to_changes(self):
        """Limit the contained problems to only those changed
        in the DiffCollection
        """
        changes = self._changes

        def sieve(err):
            if not hasattr(err, 'filename'):
                return True
            if changes.has_line_changed(err.filename, err.line):
                return True
            return False

        items = OrderedDict()
        for error in self:
            if sieve(error):
                items[error.key()] = error
        self._items = items

    def remove(self, comment):
        """Remove a problem from the list based on the filename
        position and comment.
        """
        found = False
        for i, item in self._items.items():
            if item == comment:
                found = i
                break
        if found is not False:
            del self._items[found]

    def __len__(self):
        return len(self._items)

    def __iter__(self):
        for item in self._items.values():
            yield item
