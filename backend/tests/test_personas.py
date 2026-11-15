import pytest

from scity.personas import persona_for, situation
from scity.spaces import Context


@pytest.fixture
def campus(spaces):
    class Campus:
        pass
    c = Campus()
    c.student, c.teacher = spaces.ensure_user("ou_s", "林晓"), spaces.ensure_user("ou_t", "陈老师")
    c.school = spaces.create_space("school", "甲学校")
    c.klass = spaces.create_space("class", "一班", parent_id=c.school)
    c.topic = spaces.create_workspace(c.klass, "topic", "沙盘")
    c.opc = spaces.create_space("opc", "晓光工作室")
    c.project = spaces.create_workspace(c.opc, "project", "产业园数字沙盘")
    spaces.create_workspace(spaces.create_space("class", "二班", parent_id=c.school), "topic", "别班的课题")
    spaces.add_member(c.student, c.klass, "student")
    spaces.add_member(c.student, c.opc, "member")
    spaces.add_member(c.teacher, c.klass, "teacher")
    return c


def test_persona_follows_registered_role_and_place(spaces, campus):
    assert persona_for(spaces.context(campus.student, None)).key == "assistant"
    assert persona_for(spaces.context(campus.student, campus.topic)).key == "student"
    assert persona_for(spaces.context(campus.teacher, campus.topic)).key == "teaching_assistant"
    assert persona_for(spaces.context(campus.teacher, None)).key == "assistant"  # 离开班级就是普通助手
    # 经营空间的岗位设定第二期再做，先用通用助手
    assert persona_for(spaces.context(campus.student, campus.project)).key == "assistant"


def test_student_and_teaching_assistant_are_visibly_different():
    student = persona_for(Context(1, 2, 3, "student", "learning"))
    assistant = persona_for(Context(1, 2, 3, "teacher", "learning"))
    assert (student.title, assistant.title) == ("SCity 助手", "SCity 助教")
    assert student.prompt != assistant.prompt


def test_situation_states_who_where_and_role_from_the_backend(spaces, campus):
    text = situation(spaces, spaces.context(campus.student, campus.topic))
    assert "林晓" in text and "甲学校 / 一班" in text and "沙盘" in text and "学生" in text
    text = situation(spaces, spaces.context(campus.teacher, campus.topic))
    assert "陈老师" in text and "老师" in text and "学生" not in text.split("【")[1]


def test_task_list_has_names_from_own_spaces_only(spaces, campus):
    text = situation(spaces, spaces.context(campus.student, campus.topic))
    assert "课题「沙盘」" in text and "项目「产业园数字沙盘」" in text
    assert "别班的课题" not in text
    assert "当前" in text.split("课题「沙盘」")[1].split("\n")[0]  # 标出现在所在的工作区
    main = situation(spaces, spaces.context(campus.student, None))
    assert "主聊天" in main and "课题「沙盘」" in main
    nobody = spaces.ensure_user("ou_new")
    assert "没有进行中的课题或项目" in situation(spaces, spaces.context(nobody, None))
