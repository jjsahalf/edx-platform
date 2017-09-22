"""
Grades related signals.
"""
from contextlib import contextmanager
from logging import getLogger

from courseware.model_data import get_score, set_score
from crum import get_current_user
from django.dispatch import receiver
from eventtracking import tracker
from lms.djangoapps.instructor_task.tasks_helper.module_state import GRADES_OVERRIDE_EVENT_TYPE
from openedx.core.djangoapps.course_groups.signals.signals import COHORT_MEMBERSHIP_UPDATED
from openedx.core.lib.grade_utils import is_score_higher_or_equal
from student.models import user_by_anonymous_id
from student.signals import ENROLLMENT_TRACK_UPDATED
from submissions.models import score_reset, score_set
from track.event_transaction_utils import (
    create_new_event_transaction_id,
    get_event_transaction_id,
    get_event_transaction_type,
    set_event_transaction_type
)
from util.date_utils import to_timestamp
from xblock.scorable import ScorableXBlockMixin, Score

from .signals import (
    PROBLEM_RAW_SCORE_CHANGED,
    PROBLEM_WEIGHTED_SCORE_CHANGED,
    SCORE_PUBLISHED,
    SUBSECTION_SCORE_CHANGED,
    SUBSECTION_OVERRIDE_CHANGED,
)
from ..constants import ScoreDatabaseTableEnum
from ..course_grade_factory import CourseGradeFactory
from ..scores import weighted_score
from ..tasks import RECALCULATE_GRADE_DELAY, recalculate_subsection_grade_v3

log = getLogger(__name__)

# define values to be used in grading events
GRADES_RESCORE_EVENT_TYPE = 'edx.grades.problem.rescored'
PROBLEM_SUBMITTED_EVENT_TYPE = 'edx.grades.problem.submitted'
SUBSECTION_OVERRIDE_EVENT_TYPE = 'edx.grades.subsection.score_overridden'
STATE_DELETED_EVENT_TYPE = 'edx.grades.problem.state_deleted'


@receiver(score_set)
def submissions_score_set_handler(sender, **kwargs):  # pylint: disable=unused-argument
    """
    Consume the score_set signal defined in the Submissions API, and convert it
    to a PROBLEM_WEIGHTED_SCORE_CHANGED signal defined in this module. Converts the
    unicode keys for user, course and item into the standard representation for the
    PROBLEM_WEIGHTED_SCORE_CHANGED signal.

    This method expects that the kwargs dictionary will contain the following
    entries (See the definition of score_set):
      - 'points_possible': integer,
      - 'points_earned': integer,
      - 'anonymous_user_id': unicode,
      - 'course_id': unicode,
      - 'item_id': unicode
    """
    points_possible = kwargs['points_possible']
    points_earned = kwargs['points_earned']
    course_id = kwargs['course_id']
    usage_id = kwargs['item_id']
    user = user_by_anonymous_id(kwargs['anonymous_user_id'])
    if user is None:
        return
    if points_possible == 0:
        # This scenario is known to not succeed, see TNL-6559 for details.
        return

    PROBLEM_WEIGHTED_SCORE_CHANGED.send(
        sender=None,
        weighted_earned=points_earned,
        weighted_possible=points_possible,
        user_id=user.id,
        anonymous_user_id=kwargs['anonymous_user_id'],
        course_id=course_id,
        usage_id=usage_id,
        modified=kwargs['created_at'],
        score_db_table=ScoreDatabaseTableEnum.submissions,
    )


@receiver(score_reset)
def submissions_score_reset_handler(sender, **kwargs):  # pylint: disable=unused-argument
    """
    Consume the score_reset signal defined in the Submissions API, and convert
    it to a PROBLEM_WEIGHTED_SCORE_CHANGED signal indicating that the score
    has been set to 0/0. Converts the unicode keys for user, course and item
    into the standard representation for the PROBLEM_WEIGHTED_SCORE_CHANGED signal.

    This method expects that the kwargs dictionary will contain the following
    entries (See the definition of score_reset):
      - 'anonymous_user_id': unicode,
      - 'course_id': unicode,
      - 'item_id': unicode
    """
    course_id = kwargs['course_id']
    usage_id = kwargs['item_id']
    user = user_by_anonymous_id(kwargs['anonymous_user_id'])
    if user is None:
        return

    PROBLEM_WEIGHTED_SCORE_CHANGED.send(
        sender=None,
        weighted_earned=0,
        weighted_possible=0,
        user_id=user.id,
        anonymous_user_id=kwargs['anonymous_user_id'],
        course_id=course_id,
        usage_id=usage_id,
        modified=kwargs['created_at'],
        score_deleted=True,
        score_db_table=ScoreDatabaseTableEnum.submissions,
    )


@contextmanager
def disconnect_submissions_signal_receiver(signal):
    """
    Context manager to be used for temporarily disconnecting edx-submission's set or reset signal.
    """
    if signal == score_set:
        handler = submissions_score_set_handler
    else:
        if signal != score_reset:
            raise ValueError("This context manager only deal with score_set and score_reset signals.")
        handler = submissions_score_reset_handler

    signal.disconnect(handler)
    try:
        yield
    finally:
        signal.connect(handler)


@receiver(SCORE_PUBLISHED)
def score_published_handler(sender, block, user, raw_earned, raw_possible, only_if_higher, **kwargs):  # pylint: disable=unused-argument
    """
    Handles whenever a block's score is published.
    Returns whether the score was actually updated.
    """
    update_score = True
    if only_if_higher:
        previous_score = get_score(user.id, block.location)

        if previous_score is not None:
            prev_raw_earned, prev_raw_possible = (previous_score.grade, previous_score.max_grade)

            if not is_score_higher_or_equal(prev_raw_earned, prev_raw_possible, raw_earned, raw_possible):
                update_score = False
                log.warning(
                    u"Grades: Rescore is not higher than previous: "
                    u"user: {}, block: {}, previous: {}/{}, new: {}/{} ".format(
                        user, block.location, prev_raw_earned, prev_raw_possible, raw_earned, raw_possible,
                    )
                )

    if update_score:
        # Set the problem score in CSM.
        score_modified_time = set_score(user.id, block.location, raw_earned, raw_possible)

        # Set the problem score on the xblock.
        if isinstance(block, ScorableXBlockMixin):
            block.set_score(Score(raw_earned=raw_earned, raw_possible=raw_possible))

        # Fire a signal (consumed by enqueue_subsection_update, below)
        PROBLEM_RAW_SCORE_CHANGED.send(
            sender=None,
            raw_earned=raw_earned,
            raw_possible=raw_possible,
            weight=getattr(block, 'weight', None),
            user_id=user.id,
            course_id=unicode(block.location.course_key),
            usage_id=unicode(block.location),
            only_if_higher=only_if_higher,
            modified=score_modified_time,
            score_db_table=ScoreDatabaseTableEnum.courseware_student_module,
            score_deleted=kwargs.get('score_deleted', False),
        )
    return update_score


@receiver(PROBLEM_RAW_SCORE_CHANGED)
def problem_raw_score_changed_handler(sender, **kwargs):  # pylint: disable=unused-argument
    """
    Handles the raw score changed signal, converting the score to a
    weighted score and firing the PROBLEM_WEIGHTED_SCORE_CHANGED signal.
    """
    if kwargs['raw_possible'] is not None:
        weighted_earned, weighted_possible = weighted_score(
            kwargs['raw_earned'],
            kwargs['raw_possible'],
            kwargs['weight'],
        )
    else:  # TODO: remove as part of TNL-5982
        weighted_earned, weighted_possible = kwargs['raw_earned'], kwargs['raw_possible']

    PROBLEM_WEIGHTED_SCORE_CHANGED.send(
        sender=None,
        weighted_earned=weighted_earned,
        weighted_possible=weighted_possible,
        user_id=kwargs['user_id'],
        course_id=kwargs['course_id'],
        usage_id=kwargs['usage_id'],
        only_if_higher=kwargs['only_if_higher'],
        score_deleted=kwargs.get('score_deleted', False),
        modified=kwargs['modified'],
        score_db_table=kwargs['score_db_table'],
    )


@receiver(PROBLEM_WEIGHTED_SCORE_CHANGED)
@receiver(SUBSECTION_OVERRIDE_CHANGED)
def enqueue_subsection_update(sender, **kwargs):  # pylint: disable=unused-argument
    """
    Handles the PROBLEM_WEIGHTED_SCORE_CHANGED or SUBSECTION_OVERRIDE_CHANGED signals by
    enqueueing a subsection update operation to occur asynchronously.
    """
    _emit_event(kwargs)
    result = recalculate_subsection_grade_v3.apply_async(
        kwargs=dict(
            user_id=kwargs['user_id'],
            anonymous_user_id=kwargs.get('anonymous_user_id'),
            course_id=kwargs['course_id'],
            usage_id=kwargs['usage_id'],
            only_if_higher=kwargs.get('only_if_higher'),
            expected_modified_time=to_timestamp(kwargs['modified']),
            score_deleted=kwargs.get('score_deleted', False),
            event_transaction_id=unicode(get_event_transaction_id()),
            event_transaction_type=unicode(get_event_transaction_type()),
            score_db_table=kwargs['score_db_table'],
        ),
        countdown=RECALCULATE_GRADE_DELAY,
    )


@receiver(SUBSECTION_SCORE_CHANGED)
def recalculate_course_grade_only(sender, course, course_structure, user, **kwargs):  # pylint: disable=unused-argument
    """
    Updates a saved course grade, but does not update the subsection
    grades the user has in this course.
    """
    CourseGradeFactory().update(user, course=course, course_structure=course_structure)


@receiver(ENROLLMENT_TRACK_UPDATED)
@receiver(COHORT_MEMBERSHIP_UPDATED)
def force_recalculate_course_and_subsection_grades(sender, user, course_key, **kwargs):
    """
    Updates a saved course grade, forcing the subsection grades
    from which it is calculated to update along the way.
    """
    if CourseGradeFactory().read(user, course_key=course_key):
        CourseGradeFactory().update(user=user, course_key=course_key, force_update_subsections=True)


def _emit_event(kwargs):
    """
    Emits a problem submitted event only if there is no current event
    transaction type, i.e. we have not reached this point in the code via a
    rescore or student state deletion.

    If the event transaction type has already been set and the transacation is
    a rescore, emits a problem rescored event.
    """
    root_type = get_event_transaction_type()

    if not root_type:
        root_id = get_event_transaction_id()
        if not root_id:
            root_id = create_new_event_transaction_id()
        set_event_transaction_type(PROBLEM_SUBMITTED_EVENT_TYPE)
        tracker.emit(
            unicode(PROBLEM_SUBMITTED_EVENT_TYPE),
            {
                'user_id': unicode(kwargs['user_id']),
                'course_id': unicode(kwargs['course_id']),
                'problem_id': unicode(kwargs['usage_id']),
                'event_transaction_id': unicode(root_id),
                'event_transaction_type': unicode(PROBLEM_SUBMITTED_EVENT_TYPE),
                'weighted_earned': kwargs.get('weighted_earned'),
                'weighted_possible': kwargs.get('weighted_possible'),
            }
        )

    if root_type in [GRADES_RESCORE_EVENT_TYPE, GRADES_OVERRIDE_EVENT_TYPE]:
        current_user = get_current_user()
        instructor_id = getattr(current_user, 'id', None)
        tracker.emit(
            unicode(GRADES_RESCORE_EVENT_TYPE),
            {
                'course_id': unicode(kwargs['course_id']),
                'user_id': unicode(kwargs['user_id']),
                'problem_id': unicode(kwargs['usage_id']),
                'new_weighted_earned': kwargs.get('weighted_earned'),
                'new_weighted_possible': kwargs.get('weighted_possible'),
                'only_if_higher': kwargs.get('only_if_higher'),
                'instructor_id': unicode(instructor_id),
                'event_transaction_id': unicode(get_event_transaction_id()),
                'event_transaction_type': unicode(root_type),
            }
        )

    if root_type in [SUBSECTION_OVERRIDE_EVENT_TYPE]:
        tracker.emit(
            unicode(SUBSECTION_OVERRIDE_EVENT_TYPE),
            {
                'course_id': unicode(kwargs['course_id']),
                'user_id': unicode(kwargs['user_id']),
                'problem_id': unicode(kwargs['usage_id']),
                'only_if_higher': kwargs.get('only_if_higher'),
                'override_deleted': kwargs.get('score_deleted', False),
                'event_transaction_id': unicode(get_event_transaction_id()),
                'event_transaction_type': unicode(root_type),
            }
        )
