"""Create explicitly local, synthetic preview data; never use in production."""
import os
from datetime import datetime, timedelta, timezone
os.environ.setdefault('DATABASE_URL', 'sqlite:///./preview.db')
from app import engine, SessionLocal, password_hash
from models import Base, User, Assignment, Member, Submission, ReviewTask
from domain import allocate, content_hash

Base.metadata.create_all(engine)
with SessionLocal() as db:
    if not db.query(User).filter_by(username='preview_teacher').first():
        teacher = User(username='preview_teacher', display_name='林老师', role='TEACHER', password_hash=password_hash('preview123456'))
        students = [User(username=f'preview_student{i}', display_name=f'演示学生{i}', role='STUDENT', password_hash=password_hash('preview123456')) for i in range(1, 6)]
        db.add_all([teacher, *students]); db.flush()
        now = datetime.now(timezone.utc)
        assignment = Assignment(teacher_id=teacher.id, title='人工智能与教育：案例分析', description='请选择一个人工智能在教育中的应用案例，分析它的价值、潜在风险与改进方向。请结合具体情境，论证观点并给出自己的思考。', rubric=['内容完整性','逻辑与表达','分析与创新'], submit_deadline=now+timedelta(days=7), review_deadline=now+timedelta(days=14))
        db.add(assignment); db.flush()
        db.add_all([Member(assignment_id=assignment.id, student_id=s.id) for s in students]); db.commit()
    if not db.query(Assignment).filter_by(title='案例分析：互评演示').first():
        teacher = db.query(User).filter_by(username='preview_teacher').one()
        students = [db.query(User).filter_by(username=f'preview_student{i}').one() for i in range(1, 6)]
        now = datetime.now(timezone.utc)
        assignment = Assignment(teacher_id=teacher.id, title='案例分析：互评演示', description='阅读同伴的案例分析，结合具体原文提出建议。', rubric=['内容完整性','逻辑与表达','分析与创新'], submit_deadline=now-timedelta(days=1), review_deadline=now+timedelta(days=7), status='REVIEWING', allocation_seed='42')
        db.add(assignment); db.flush()
        db.add_all([Member(assignment_id=assignment.id, student_id=s.id) for s in students])
        submissions = {}
        for i, student in enumerate(students):
            text = f'案例 {i+1}：人工智能辅助学习。😀\n\n人工智能可以根据学生的练习情况推荐不同难度的题目，帮助学生及时发现知识漏洞。这样的方式能够提高练习效率，也让教师更容易了解班级的学习情况。\n\n不过，如果系统收集了大量学习记录，就可能带来隐私风险。学校需要明确数据用途，减少不必要的信息收集，并让学生知道自己的数据如何被使用。'
            submission = Submission(assignment_id=assignment.id, student_id=student.id, content_type='TEXT', content_text=text, content_hash=content_hash(text))
            db.add(submission); submissions[student.id] = submission
        db.flush()
        for owner, reviewer in allocate([s.id for s in students], 42):
            db.add(ReviewTask(assignment_id=assignment.id, submission_id=submissions[owner].id, reviewer_id=reviewer))
        db.commit()
print('Synthetic preview accounts: preview_teacher / preview_student1 through 5; password: preview123456')
