"""Create synthetic examples in a local preview database."""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault('DATABASE_URL', 'sqlite:///./preview.db')
from app import engine, SessionLocal, password_hash
from models import User, Assignment, Member, Submission, ReviewTask, Review
from db_migrations import upgrade_database
from domain import allocate, content_hash, score_review

RUBRIC = ['内容完整性', '逻辑与表达', '分析与创新']
PASSWORD = 'preview123456'
QUOTE = '人工智能可以辅助学习。'


def add_example(db, teacher, students, title, description, status, submit_days, review_days, now):
    if db.query(Assignment).filter_by(teacher_id=teacher.id, title=title).first():
        return
    assignment = Assignment(teacher_id=teacher.id, title=title, description=description, rubric=RUBRIC,
                            submit_deadline=now + timedelta(days=submit_days),
                            review_deadline=now + timedelta(days=review_days), status=status)
    db.add(assignment)
    db.flush()
    db.add_all(Member(assignment_id=assignment.id, student_id=student.id) for student in students)
    if status == 'SUBMITTING':
        return

    seed = 42
    assignment.allocation_seed = str(seed)
    submissions = {}
    for index, student in enumerate(students, start=1):
        text = f'案例 {index}：人工智能辅助学习。😀\n\n{QUOTE}\n\n人工智能可以根据学生的练习情况推荐不同难度的题目，帮助学生及时发现知识漏洞。这样的方式能够提高练习效率，也让教师更容易了解班级的学习情况。'
        text += '\n\n不过，如果系统收集了大量学习记录，就可能带来隐私风险。学校需要明确数据用途，减少不必要的信息收集，并让学生知道自己的数据如何被使用。'
        if status == 'CLOSED':
            text += ('\n\n学校可以在不同班级记录题目完成时间和错题率，并在比较结果前说明样本来源。'
                     '试用阶段可由教师审阅推荐题目，发现偏差时人工调整。') * 12
            text += f'\n\n{QUOTE}最终仍需由教师结合实际课堂情况决定如何使用。'
        submission = Submission(assignment_id=assignment.id, student_id=student.id, content_type='TEXT',
                                content_text=text, content_hash=content_hash(text))
        db.add(submission)
        submissions[student.id] = submission
    db.flush()

    reviewed = set()
    for owner, reviewer in allocate(sorted(submissions), seed):
        submission = submissions[owner]
        task = ReviewTask(assignment_id=assignment.id, submission_id=submission.id, reviewer_id=reviewer,
                          status='COMPLETED' if status == 'CLOSED' else 'PENDING')
        db.add(task)
        if status == 'CLOSED':
            db.flush()
            text = submission.content_text
            start = text.rindex(QUOTE) if owner in reviewed else text.index(QUOTE)
            reviewed.add(owner)
            scores = [4, 4, 3]
            db.add(Review(review_task_id=task.id, scores=scores, total_score=score_review(scores),
                          strengths='案例具体，说明了题目推荐如何帮助学生。',
                          weaknesses='结论需要用课堂试点数据支持。',
                          suggestions='增加一张对照表，列出试点前后的错题率和样本人数。',
                          evidence_anchors=[{'start': start, 'end': start + len(QUOTE), 'quote': QUOTE,
                                             'content_hash': submission.content_hash}],
                          submitted_at=now - timedelta(days=2)))


def main():
    if engine.dialect.name != 'sqlite' or os.getenv('APP_ENV', '').lower() == 'production':
        raise SystemExit('Preview fixtures require a local development SQLite database.')
    upgrade_database(engine)
    with SessionLocal.begin() as db:
        teacher = db.query(User).filter_by(username='preview_teacher').first()
        if teacher is None:
            teacher = User(username='preview_teacher', display_name='林老师', role='TEACHER',
                           password_hash=password_hash(PASSWORD))
            db.add(teacher)
            db.add_all(User(username=f'preview_student{i}', display_name=f'演示学生{i}', role='STUDENT',
                            password_hash=password_hash(PASSWORD)) for i in range(1, 6))
            db.flush()
        students = [db.query(User).filter_by(username=f'preview_student{i}').one() for i in range(1, 6)]
        now = datetime.now(timezone.utc)
        add_example(db, teacher, students, '人工智能与教育：案例分析',
                    '选择一个人工智能在教育中的应用案例，分析它的价值、潜在风险与改进方向。', 'SUBMITTING', 7, 14, now)
        add_example(db, teacher, students, '案例分析：互评演示',
                    '阅读同伴的案例分析，结合具体原文提出建议。', 'REVIEWING', -1, 7, now)
        add_example(db, teacher, students, '案例分析：反馈演示',
                    '查看已发布的匿名评价，点击证据定位作业原文。', 'CLOSED', -7, -1, now)
    print('Synthetic accounts: preview_teacher / preview_student1 through 5; password:', PASSWORD)


if __name__ == '__main__':
    main()
