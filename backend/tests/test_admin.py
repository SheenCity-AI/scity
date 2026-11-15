import json
from datetime import datetime

import pytest
from sqlalchemy import func, select

from scity.admin import main, open_course
from scity.models import LedgerEntry, User

SPEC = {
    "school": "甲学校",
    "class": "建筑学 2026 级 1 班",
    "course": {"name": "城市开发全过程沙盘", "ref": "sandbox@0.1"},
    "teachers": [{"open_id": "ou_t", "name": "陈老师"}],
    "students": [{"open_id": "ou_s1", "name": "林晓"}, {"open_id": "ou_s2"}],
    "subsidy": {"points": 3000, "expires": "2027-01-31"},
}


def test_open_course_builds_space_roles_workspace_and_subsidy(sessions, spaces, wallets):
    result = open_course(sessions, spaces, wallets, SPEC)
    teacher, s1, s2 = (spaces.ensure_user(x) for x in ("ou_t", "ou_s1", "ou_s2"))
    assert spaces.context(teacher, result.workspace_id).role == "teacher"
    assert spaces.context(s1, result.workspace_id).role == "student"
    assert [(w.name, w.category) for w in spaces.workspaces(s2)] == [("城市开发全过程沙盘", "learning")]
    assert wallets.view(spaces.context(s1, None)).subsidy == 3000
    assert wallets.view(spaces.context(teacher, None)).subsidy == 0
    assert (result.teachers, result.students, result.granted) == (1, 2, 2)
    with sessions() as db:
        assert db.scalar(select(User.display_name).where(User.id == s1)) == "林晓"
        from scity.models import Wallet
        wallet = db.scalar(select(Wallet).where(Wallet.user_id == s1, Wallet.space_id == result.school_id))
        assert wallet.expires_at == datetime(2027, 1, 31, 16, 0)  # 北京时间 2027-01-31 当天结束


def test_open_course_can_be_rerun_to_add_people_without_paying_twice(sessions, spaces, wallets):
    first = open_course(sessions, spaces, wallets, SPEC)
    more = dict(SPEC, students=SPEC["students"] + [{"open_id": "ou_s3"}])
    second = open_course(sessions, spaces, wallets, more)
    assert (second.class_id, second.workspace_id) == (first.class_id, first.workspace_id)
    assert (second.students, second.granted) == (3, 1)
    for open_id in ("ou_s1", "ou_s3"):
        assert wallets.view(spaces.context(spaces.ensure_user(open_id), None)).subsidy == 3000
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "grant")) == 3


def test_second_course_in_same_class_reuses_people_and_adds_a_workspace(sessions, spaces, wallets):
    first = open_course(sessions, spaces, wallets, SPEC)
    other = dict(SPEC, course={"name": "筑年", "ref": "zhunian@1"}, subsidy=None)
    second = open_course(sessions, spaces, wallets, other)
    assert second.class_id == first.class_id and second.workspace_id != first.workspace_id
    assert second.granted == 0
    s1 = spaces.ensure_user("ou_s1")
    assert [w.name for w in spaces.workspaces(s1)] == ["城市开发全过程沙盘", "筑年"]
    assert wallets.view(spaces.context(s1, None)).subsidy == 3000


def test_bad_roster_is_rejected_before_anything_is_created(sessions, spaces, wallets):
    for broken in (dict(SPEC, students=[{"name": "没有账号"}]), {k: v for k, v in SPEC.items() if k != "course"},
                   dict(SPEC, subsidy={"points": 0, "expires": "2027-01-31"})):
        with pytest.raises(ValueError):
            open_course(sessions, spaces, wallets, broken)
    assert spaces.find_space("school", "甲学校") is None


def test_command_line_opens_course_and_lists(tmp_path, monkeypatch, capsys, database_url, sessions):
    monkeypatch.setenv("SCITY_ENV_FILES", "/nonexistent")
    monkeypatch.setenv("SCITY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCITY_PRIVATE_DIR", "/nonexistent")
    monkeypatch.setenv("SCITY_DATABASE_URL", database_url)
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps(SPEC, ensure_ascii=False), encoding="utf-8")
    main(["open-course", str(roster)])
    assert "老师 1 人，学生 2 人，新发补贴 2 人" in capsys.readouterr().out
    main(["spaces"])
    out = capsys.readouterr().out
    assert "甲学校" in out and "建筑学 2026 级 1 班" in out and "城市开发全过程沙盘" in out and "sandbox@0.1" in out
    main(["users"])
    out = capsys.readouterr().out
    assert "林晓" in out and "student" in out and "ou_s1" not in out  # 账号只显示末 4 位
