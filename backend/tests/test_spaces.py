import pytest

from scity.spaces import NotMember, WorkspaceArchived


def test_person_can_join_several_spaces_with_different_roles(spaces):
    uid = spaces.ensure_user("ou_a")
    assert spaces.ensure_user("ou_a") == uid
    school = spaces.create_space("school", "甲学校")
    klass = spaces.create_space("class", "一班", parent_id=school)
    opc = spaces.create_space("opc", "晓光工作室")
    spaces.add_member(uid, klass, "student")
    spaces.add_member(uid, opc, "member")
    assert {(m.space_id, m.role) for m in spaces.memberships(uid)} == {(klass, "student"), (opc, "member")}


def test_role_comes_from_membership_not_from_caller(spaces):
    student, teacher = spaces.ensure_user("ou_s"), spaces.ensure_user("ou_t")
    klass = spaces.create_space("class", "一班")
    topic = spaces.create_workspace(klass, "topic", "沙盘")
    spaces.add_member(student, klass, "student")
    spaces.add_member(teacher, klass, "teacher")
    assert spaces.context(student, topic).role == "student"
    assert spaces.context(teacher, topic).role == "teacher"
    outsider = spaces.ensure_user("ou_x")
    with pytest.raises(NotMember):
        spaces.context(outsider, topic)


def test_main_chat_has_no_space(spaces):
    uid = spaces.ensure_user("ou_a")
    ctx = spaces.context(uid, None)
    assert (ctx.user_id, ctx.space_id, ctx.workspace_id, ctx.role) == (uid, None, None, None)
    assert ctx.category == "main"


def test_same_person_one_role_per_space_and_role_can_change(spaces):
    uid = spaces.ensure_user("ou_a")
    klass = spaces.create_space("class", "一班")
    spaces.add_member(uid, klass, "student")
    spaces.add_member(uid, klass, "teacher")
    assert [(m.space_id, m.role) for m in spaces.memberships(uid)] == [(klass, "teacher")]


def test_leaving_a_space_removes_access_but_keeps_the_person(spaces):
    uid = spaces.ensure_user("ou_a")
    klass = spaces.create_space("class", "一班")
    topic = spaces.create_workspace(klass, "topic", "沙盘")
    spaces.add_member(uid, klass, "student")
    spaces.leave(uid, klass)
    assert spaces.memberships(uid) == []
    with pytest.raises(NotMember):
        spaces.context(uid, topic)
    assert spaces.context(uid, None).user_id == uid
    spaces.add_member(uid, klass, "student")
    assert spaces.context(uid, topic).role == "student"


def test_archived_workspace_is_read_only(spaces):
    uid = spaces.ensure_user("ou_a")
    klass = spaces.create_space("class", "一班")
    topic = spaces.create_workspace(klass, "topic", "沙盘")
    spaces.add_member(uid, klass, "student")
    spaces.archive_workspace(topic)
    with pytest.raises(WorkspaceArchived):
        spaces.context(uid, topic)
    assert [w.id for w in spaces.workspaces(uid)] == []
    assert [w.id for w in spaces.workspaces(uid, include_archived=True)] == [topic]


def test_task_list_covers_all_spaces_with_names_only(spaces):
    uid = spaces.ensure_user("ou_a")
    klass = spaces.create_space("class", "一班")
    opc = spaces.create_space("opc", "晓光工作室")
    a = spaces.create_workspace(klass, "topic", "沙盘")
    b = spaces.create_workspace(opc, "project", "产业园数字沙盘")
    spaces.create_workspace(spaces.create_space("class", "二班"), "topic", "别班的课题")
    spaces.add_member(uid, klass, "student")
    spaces.add_member(uid, opc, "member")
    assert [(w.id, w.name, w.category) for w in spaces.workspaces(uid)] == [(a, "沙盘", "learning"), (b, "产业园数字沙盘", "business")]


def test_invalid_kind_and_role_are_rejected(spaces):
    uid = spaces.ensure_user("ou_a")
    with pytest.raises(ValueError):
        spaces.create_space("club", "x")
    klass = spaces.create_space("class", "一班")
    with pytest.raises(ValueError):
        spaces.add_member(uid, klass, "boss")
    with pytest.raises(ValueError):
        spaces.create_workspace(klass, "project", "班级里不能建项目")
