from datetime import datetime, timedelta

import pytest

from scity.course import Stage
from scity.game import GameError, GameService
from scity.spaces import NotMember


class ToyCourse:
    """最小的课程插件：开场 → 暗标（限时填报）→ 公布。"""

    ref = "toy@1"
    stages = [Stage("intro", "开场", 1), Stage("bid", "暗标", 1, minutes=10, ends="timer_or_all", form=True, extend_minutes=5),
              Stage("reveal", "公布", 1), Stage("sealed", "限时", 2, minutes=5, ends="timer", form=True)]

    def validate(self, stage, payload, state, team):
        if not isinstance(payload.get("price"), int) or payload["price"] < 100:
            raise ValueError("报价不能低于 100")
        return {"price": payload["price"]}

    def default(self, stage, state, team):
        return {"price": 150} if state.team(team).npc else None

    def validate_record(self, stage, payload, state):
        if stage != "reveal":
            raise ValueError("这个阶段没有要录入的内容")
        return payload

    def close(self, stage, state):
        if stage != "bid":
            return None
        bids = {t: d["price"] for t, d in state.decisions.get("bid", {}).items()}
        return {"winner": max(bids, key=bids.get) if bids else None, "bids": bids}

    def role(self, stage, state, team):
        """暗标阶段可以问拍卖师，每组两次。"""
        if stage != "bid":
            return None
        return {"name": "拍卖师", "prompt": f"你是拍卖师，在和{team}说话。只讲规则，不透露别人的报价。", "turns": 2}

    def view(self, state, team, role):
        result = state.results.get("bid")
        if role == "teacher":
            return {"bids": state.decisions.get("bid", {}), "result": result}
        mine = state.decisions.get("bid", {}).get(team)
        return {"mine": mine, "winner": result["winner"] if result and state.stage != "bid" else None}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 20, 9, 0)

    def __call__(self):
        return self.now

    def forward(self, minutes):
        self.now += timedelta(minutes=minutes)


@pytest.fixture
def room(sessions, spaces):
    class Room:
        pass
    r = Room()
    r.clock = Clock()
    r.games = GameService(sessions, {"toy@1": ToyCourse()}, now=r.clock)
    r.teacher, r.a, r.b, r.c = (spaces.ensure_user(x) for x in ("ou_t", "ou_a", "ou_b", "ou_c"))
    klass = spaces.create_space("class", "一班")
    r.workspace = spaces.create_workspace(klass, "topic", "玩具课", "toy@1")
    spaces.add_member(r.teacher, klass, "teacher")
    for user in (r.a, r.b, r.c):
        spaces.add_member(user, klass, "student")
    r.ctx = lambda user: spaces.context(user, r.workspace)
    r.game = r.games.create(r.workspace, "toy@1", seed="s1", teams=[
        {"name": "第1组", "members": [r.a, r.b]}, {"name": "第2组", "members": [r.c]}, {"name": "对手", "npc": True}])
    return r


def test_new_game_waits_for_teacher_and_everyone_sees_the_same_stage(room):
    view = room.games.view(room.game, room.ctx(room.a))
    assert (view["stage"], view["team"], view["role"]) == (None, "第1组", "student")
    assert [s["key"] for s in view["stages"]] == ["intro", "bid", "reveal", "sealed"]
    room.games.advance(room.game, room.ctx(room.teacher))
    for user in (room.a, room.c, room.teacher):
        assert room.games.view(room.game, room.ctx(user))["stage"]["key"] == "intro"
    assert room.games.view(room.game, room.ctx(room.teacher))["team"] is None


def test_only_the_registered_teacher_moves_the_game(room, spaces):
    with pytest.raises(GameError, match="只有老师"):
        room.games.advance(room.game, room.ctx(room.a))
    outsider = spaces.ensure_user("ou_x")
    with pytest.raises(NotMember):
        room.games.advance(room.game, spaces.context(outsider, room.workspace))
    assert room.games.view(room.game, room.ctx(room.a))["stage"] is None


def test_timed_form_stage_accepts_until_deadline_and_keeps_history(room):
    teacher = room.ctx(room.teacher)
    room.games.advance(room.game, teacher)
    with pytest.raises(GameError, match="不需要填报"):
        room.games.submit(room.game, room.ctx(room.a), {"price": 200})
    room.games.advance(room.game, teacher)
    view = room.games.view(room.game, room.ctx(room.a))
    assert view["stage"]["deadline"] == "2026-11-20T09:10:00Z" and view["now"] == "2026-11-20T09:00:00Z"
    with pytest.raises(GameError, match="不能低于"):
        room.games.submit(room.game, room.ctx(room.a), {"price": 50})
    room.games.submit(room.game, room.ctx(room.a), {"price": 200})
    room.games.submit(room.game, room.ctx(room.b), {"price": 260})  # 同组的人改了报价，以最后一次为准
    assert room.games.view(room.game, room.ctx(room.a))["course"]["mine"] == {"price": 260}
    room.clock.forward(11)
    with pytest.raises(GameError, match="已经截止"):
        room.games.submit(room.game, room.ctx(room.c), {"price": 300})
    assert len(room.games.state(room.game).history("bid", "第1组")) == 2


def test_students_cannot_see_others_bids_but_teacher_can(room):
    teacher = room.ctx(room.teacher)
    room.games.advance(room.game, teacher)
    room.games.advance(room.game, teacher)
    room.games.submit(room.game, room.ctx(room.a), {"price": 200})
    student = room.games.view(room.game, room.ctx(room.c))
    assert student["course"] == {"mine": None, "winner": None}
    assert "submitted" not in student
    seen = room.games.view(room.game, teacher)
    assert seen["course"]["bids"] == {"第1组": {"price": 200}}
    assert seen["submitted"] == {"第1组": True, "第2组": False}


def test_teacher_can_end_early_only_when_all_teams_are_in(room):
    teacher = room.ctx(room.teacher)
    room.games.advance(room.game, teacher)
    room.games.advance(room.game, teacher)
    room.games.submit(room.game, room.ctx(room.a), {"price": 200})
    with pytest.raises(GameError, match="还有组没提交"):
        room.games.advance(room.game, teacher)
    room.games.submit(room.game, room.ctx(room.c), {"price": 120})
    room.games.advance(room.game, teacher)
    state = room.games.state(room.game)
    assert state.stage == "reveal"
    # 关闭阶段时：系统对手按默认规则出价，结果由课程插件算出
    assert state.results["bid"] == {"winner": "第1组", "bids": {"第1组": 200, "第2组": 120, "对手": 150}}
    assert room.games.view(room.game, room.ctx(room.c))["course"]["winner"] == "第1组"


def test_after_deadline_missing_teams_get_defaults_and_stage_can_close(room):
    teacher = room.ctx(room.teacher)
    room.games.advance(room.game, teacher)
    room.games.advance(room.game, teacher)
    room.clock.forward(10)
    room.games.advance(room.game, teacher)
    state = room.games.state(room.game)
    assert state.results["bid"]["bids"] == {"对手": 150}  # 学生组没有默认报价，视为放弃
    assert state.defaulted == {("bid", "对手")}  # 课程插件看得出哪些是系统代填的（比如打分时区分“没交”）


def test_a_real_submission_is_not_marked_as_defaulted(room):
    teacher = room.ctx(room.teacher)
    room.games.advance(room.game, teacher)
    room.games.advance(room.game, teacher)
    room.games.submit(room.game, room.ctx(room.a), {"price": 300})
    room.clock.forward(10)
    room.games.advance(room.game, teacher)
    state = room.games.state(room.game)
    assert ("bid", "第1组") not in state.defaulted and state.decisions["bid"]["第1组"] == {"price": 300}


def test_strict_timer_stage_cannot_be_ended_early_and_extension_is_capped(room):
    teacher = room.ctx(room.teacher)
    for _ in range(2):
        room.games.advance(room.game, teacher)
    room.games.extend(room.game, teacher, 5)
    assert room.games.view(room.game, teacher)["stage"]["deadline"] == "2026-11-20T09:15:00Z"
    with pytest.raises(GameError, match="最多还能延长 0 分钟"):
        room.games.extend(room.game, teacher, 1)
    room.clock.forward(15)
    room.games.advance(room.game, teacher)
    room.games.advance(room.game, teacher)  # 进入“限时”
    room.games.submit(room.game, room.ctx(room.a), {"price": 100})
    room.games.submit(room.game, room.ctx(room.c), {"price": 100})
    with pytest.raises(GameError, match="倒计时结束前不能"):
        room.games.advance(room.game, teacher)
    room.clock.forward(5)
    room.games.advance(room.game, teacher)
    view = room.games.view(room.game, teacher)
    assert view["stage"] is None and view["finished"] is True
    with pytest.raises(GameError, match="已经结束"):
        room.games.advance(room.game, teacher)


def test_teacher_records_are_validated_ordered_and_bump_version(room):
    teacher = room.ctx(room.teacher)
    before = room.games.view(room.game, teacher)["version"]
    room.games.advance(room.game, teacher)
    with pytest.raises(GameError, match="没有要录入"):
        room.games.record(room.game, teacher, {"x": 1})
    with pytest.raises(GameError, match="只有老师"):
        room.games.record(room.game, room.ctx(room.a), {"x": 1})
    room.games.advance(room.game, teacher)
    room.clock.forward(10)
    room.games.advance(room.game, teacher)
    room.games.record(room.game, teacher, {"lot": "A", "price": 1})
    room.games.record(room.game, teacher, {"lot": "A", "price": 2})  # 录错了，改一次
    assert room.games.state(room.game).records["reveal"] == [{"lot": "A", "price": 1}, {"lot": "A", "price": 2}]
    assert room.games.view(room.game, teacher)["version"] > before
    assert room.games.version(room.game) == room.games.view(room.game, teacher)["version"]


def test_one_person_one_team_and_new_game_archives_the_old_one(room):
    with pytest.raises(GameError, match="同时在两个组"):
        room.games.create(room.workspace, "toy@1", seed="s2", teams=[
            {"name": "甲", "members": [room.a]}, {"name": "乙", "members": [room.a]}])
    again = room.games.create(room.workspace, "toy@1", seed="s2", teams=[{"name": "甲", "members": [room.a]}])
    assert room.games.active(room.workspace) == again != room.game
    with pytest.raises(GameError, match="已经归档"):
        room.games.advance(room.game, room.ctx(room.teacher))
    with pytest.raises(GameError, match="不在这一局的任何组"):
        room.games.submit(again, room.ctx(room.c), {"price": 100})


def test_course_can_narrow_which_teams_a_stage_waits_for(room, monkeypatch):
    teacher = room.ctx(room.teacher)
    monkeypatch.setattr(ToyCourse, "required_teams", lambda self, stage, state: ["第2组"], raising=False)
    room.games.advance(room.game, teacher)
    room.games.advance(room.game, teacher)
    assert room.games.view(room.game, teacher)["submitted"] == {"第2组": False}
    room.games.submit(room.game, room.ctx(room.c), {"price": 120})
    room.games.advance(room.game, teacher)  # 第 1 组没交也能提前结束
    assert room.games.state(room.game).stage == "reveal"


def test_courses_are_loaded_from_the_private_directory(tmp_path):
    from scity.course import load_courses
    folder = tmp_path / "courses" / "demo"
    folder.mkdir(parents=True)
    (folder / "plugin.py").write_text("class C:\n    ref = 'demo@1'\n    stages = []\nCOURSE = C()\n", encoding="utf-8")
    assert list(load_courses(tmp_path)) == ["demo@1"]
    assert load_courses(tmp_path / "nothing") == {}


def test_course_knows_how_long_the_current_stage_has_been_open(room):
    teacher = room.ctx(room.teacher)
    room.games.advance(room.game, teacher)
    room.games.advance(room.game, teacher)
    room.clock.forward(7)
    assert room.games.state(room.game).stage_minutes == 7


def test_brief_for_the_assistant_is_role_aware_and_optional(room, spaces, monkeypatch):
    student, teacher = room.ctx(room.a), room.ctx(room.teacher)
    assert room.games.brief(student) == ""  # 课程没有提供摘要
    assert room.games.brief(spaces.context(room.a, None)) == ""  # 主聊天没有课题现场
    monkeypatch.setattr(ToyCourse, "brief", lambda self, state, team, role: f"阶段 {state.stage}，{role}，{team}", raising=False)
    room.games.advance(room.game, teacher)
    assert room.games.brief(student) == "阶段 intro，student，第1组"
    assert room.games.brief(teacher) == "阶段 intro，teacher，None"
    with pytest.raises(NotMember):
        room.games.brief(spaces.context(spaces.ensure_user("ou_x"), room.workspace))



def test_talking_to_a_course_role_is_limited_per_team_and_stage(room):
    teacher, a, b, c = (room.ctx(u) for u in (room.teacher, room.a, room.b, room.c))
    room.games.advance(room.game, teacher)
    with pytest.raises(GameError, match="没有可以对话的角色"):
        room.games.talk_seat(room.game, a)  # 开场阶段没有角色
    room.games.advance(room.game, teacher)
    with pytest.raises(GameError, match="不在这一局的任何组里"):
        room.games.talk_seat(room.game, teacher)
    seat = room.games.talk_seat(room.game, a)
    assert (seat.stage, seat.team, seat.name, seat.left, seat.history) == ("bid", "第1组", "拍卖师", 2, ())
    assert "第1组" in seat.prompt
    before = room.games.version(room.game)
    assert room.games.talk_save(room.game, a, "bid", "最低报多少？", "不能低于 100。") == 1
    assert room.games.version(room.game) == before + 1  # 同组的另一台电脑会刷新出这段对话
    seat = room.games.talk_seat(room.game, b)  # 同组的另一个人：接着同一段对话，次数一起算
    assert seat.left == 1 and seat.history == (("最低报多少？", "不能低于 100。"),)
    assert room.games.talk_save(room.game, b, "bid", "能改吗？", "截止前可以改。") == 0
    with pytest.raises(GameError, match="对话次数已经用完"):
        room.games.talk_seat(room.game, a)
    with pytest.raises(GameError, match="对话次数已经用完"):
        room.games.talk_save(room.game, a, "bid", "再问一次", "……")
    assert room.games.talk_seat(room.game, c).left == 2  # 别的组不受影响
    state = room.games.state(room.game)
    assert state.talks == {"bid": {"第1组": [{"q": "最低报多少？", "a": "不能低于 100。"}, {"q": "能改吗？", "a": "截止前可以改。"}]}}


def test_talk_is_refused_after_the_deadline_or_when_the_stage_moved_on(room):
    teacher, c = room.ctx(room.teacher), room.ctx(room.c)
    room.games.advance(room.game, teacher)
    room.games.advance(room.game, teacher)
    seat = room.games.talk_seat(room.game, c)
    room.clock.forward(10)
    with pytest.raises(GameError, match="已经截止"):
        room.games.talk_seat(room.game, c)
    room.games.advance(room.game, teacher)
    with pytest.raises(GameError, match="这个阶段已经结束"):
        room.games.talk_save(room.game, c, seat.stage, "还来得及吗？", "……")  # 模型回答期间老师点了下一步：不记
    assert room.games.state(room.game).talks == {}
