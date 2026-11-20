"""内容型课程包：不写程序，一个文件夹就能上课。用 tests/fixtures/sample_course 从头走到尾。"""
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from scity.content_course import ContentCourse, CourseFormatError, problems
from scity.course import load_courses
from scity.game import GameError, GameService
from tests.test_game import Clock

SAMPLE = Path(__file__).parent / "fixtures" / "sample_course"
SURVEY = {"选的地方": "二教一楼楼梯间", "类型": "室内", "面积": 12, "主要问题": ["太暗", "没地方坐"], "现场记录": "下午没人，堆着杂物。"}


@pytest.fixture
def room(sessions, spaces):
    class Room:
        pass
    r = Room()
    r.course = ContentCourse(SAMPLE)
    r.clock = Clock()
    r.games = GameService(sessions, {r.course.ref: r.course}, now=r.clock)
    r.teacher, r.a, r.b, r.c = (spaces.ensure_user(x) for x in ("ou_t", "ou_a", "ou_b", "ou_c"))
    klass = spaces.create_space("class", "一班")
    ws = spaces.create_workspace(klass, "topic", "校园一角", r.course.ref)
    spaces.add_member(r.teacher, klass, "teacher")
    for user in (r.a, r.b, r.c):
        spaces.add_member(user, klass, "student")
    r.ctx = lambda user: spaces.context(user, ws)
    r.game = r.games.create(ws, r.course.ref, seed="s", teams=[{"name": "第1组", "members": [r.a, r.b]}, {"name": "第2组", "members": [r.c]}])
    r.next = lambda: r.games.advance(r.game, r.ctx(r.teacher))
    r.view = lambda user: r.games.view(r.game, r.ctx(user))
    r.submit = lambda user, payload: r.games.submit(r.game, r.ctx(user), payload)
    return r


def test_course_file_becomes_stages_tasks_and_blocks():
    course = ContentCourse(SAMPLE)
    assert course.ref == "campus-corner@0.1" and course.name == "校园一角改造"
    assert [(s.key, s.lesson, s.minutes, s.ends, s.form, s.extend_minutes) for s in course.stages] == [
        ("n01", 1, None, "teacher", False, 0), ("n02", 1, 20, "timer_or_all", True, 5), ("n03", 1, None, "teacher", True, 0),
        ("n04", 2, 30, "timer", True, 0), ("n05", 2, 10, "timer_or_all", True, 0), ("n06", 2, None, "teacher", False, 0)]
    fields = {f["key"]: f for f in course.nodes["n02"].fields}
    assert fields["类型"]["options"] == [{"v": o, "label": o} for o in ("室内", "半室外", "室外")]
    assert (fields["面积"]["min"], fields["面积"]["max"], fields["面积"]["unit"]) == (5, 60, "平方米")
    assert fields["主要问题"]["max"] == 2 and fields["主要问题"]["hint"] == "" and len(fields["主要问题"]["options"]) == 5
    assert fields["现场记录"]["max"] == 300 and fields["现场记录"]["rows"] == 5 and fields["选的地方"]["max"] == 30
    assert not fields["照片链接"]["required"] and fields["选的地方"]["hint"] == "写清在哪栋楼、哪个位置"
    assert not course.nodes["n05"].fields[1]["required"] and course.nodes["n05"].fields[1]["max"] == 200
    assert course.nodes["n05"].role["turns"] == 4 and course.nodes["n05"].role["prompt"].startswith("你扮演一位")
    assert "次数" not in course.nodes["n05"].role["prompt"]
    assert [(b["id"], b["lesson"], b["weight"], b["cards"]) for b in course.blocks] == [
        ("B1", 1, 0.4, ["K-SJ-001", "K-SJ-002"]), ("B2", 2, 0.6, ["K-SJ-003", "K-SJ-004"])]
    assert course.gate("n03") == 1 and course.gate("n06") == 2 and course.gate("n02") is None
    assert problems(SAMPLE) == []


@pytest.mark.parametrize("old, new, message", [
    ("- 编号：campus-corner", "- 编号：校园", "开头要有"),
    ("### N02 现场调研", "### 现场调研", "阶段标题要写成"),
    ("### N03 课后：定题", "### N02 课后：定题", "阶段编号 n02 重复"),
    ("| 类型 | 单选 | 室内、半室外、室外 | |", "| 类型 | 下拉 | 室内、室外 | |", "类型要写"),
    ("| 类型 | 单选 | 室内、半室外、室外 | |", "| 类型 | 单选 | 室内 | |", "至少要有两个选项"),
    ("- 结束：到点", "- 结束：随便", "“结束”要写"),
    ("#### 讲稿", "#### 备注", "只能有"),
    ("## 第 2 次课 方案和反馈", "## 方案和反馈", "二级标题要写成"),
])
def test_format_mistakes_are_reported_in_plain_words(tmp_path, old, new, message):
    shutil.copytree(SAMPLE, tmp_path / "c")
    path = tmp_path / "c" / "课程.md"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")
    with pytest.raises(CourseFormatError, match=message):
        ContentCourse(tmp_path / "c")
    assert len(problems(tmp_path / "c")) == 1


def test_acceptance_check_lists_what_is_missing(tmp_path):
    shutil.copytree(SAMPLE, tmp_path / "c")
    blocks = tmp_path / "c" / "评价板块.md"
    blocks.write_text(blocks.read_text(encoding="utf-8").replace("占总分：60%", "占总分：50%").replace("K-SJ-003、K-SJ-004", "K-SJ-001"), encoding="utf-8")
    found = problems(tmp_path / "c")
    assert any("合计 90%" in x for x in found) and any("K-SJ-001 同时在板块 B1 和 B2" in x for x in found)
    blocks.unlink()
    assert any("老师打不了分" in x for x in problems(tmp_path / "c"))


def test_loader_picks_up_content_folders_and_skips_broken_ones(tmp_path):
    shutil.copytree(SAMPLE, tmp_path / "courses" / "good")
    shutil.copytree(SAMPLE, tmp_path / "courses" / "broken")
    bad = tmp_path / "courses" / "broken" / "课程.md"
    bad.write_text(bad.read_text(encoding="utf-8").replace("### N02", "### 二"), encoding="utf-8")
    shutil.copytree(SAMPLE, tmp_path / "courses" / "twin")  # 编号和版本跟 good 一样：后来的不装
    courses = load_courses(tmp_path)
    assert list(courses) == ["campus-corner@0.1"] and isinstance(courses["campus-corner@0.1"], ContentCourse)


def test_a_whole_course_runs_from_files_alone(room):
    course, teacher = room.course, room.teacher
    start = room.view(room.a)["course"]
    assert start["hint"] == "等老师开始上课。" and start["materials"] == {} and "teacher" not in start
    room.next()
    view = room.view(room.a)["course"]
    assert [m["title"] for m in view["materials"]["n01"]] == ["课题说明"] and view["materials"]["n01"][0]["open"]
    assert "n02" not in view["materials"] and view["glossary"] == {"使用者画像": course.glossary["使用者画像"]}
    assert view["page"]["cards"][0]["body"] == "听老师介绍这门课要做什么。" and "form" not in view["page"]
    assert "讲稿" not in str(view) and "可以问：" not in str(view)  # 讲稿只给老师
    assert "可以问：" in room.view(teacher)["course"]["teacher"]["lectures"]["n01"]
    room.next()  # 现场调研
    with pytest.raises(GameError, match="请填“选的地方”"):
        room.submit(room.a, {})
    for wrong, message in (({"类型": "地下"}, "要从给出的选项里选"), ({"面积": 80}, "要在 5 到 60 之间"), ({"面积": "很大"}, "要填数字"),
                           ({"主要问题": ["太暗", "太吵", "没人管"]}, "最多选 2 项"), ({"选的地方": "很" * 31}, "最多 30 字")):
        with pytest.raises(GameError, match=message):
            room.submit(room.a, {**SURVEY, **wrong})
    room.submit(room.a, SURVEY)
    room.submit(room.b, {**SURVEY, "面积": 15.5, "照片链接": " https://example.com/p "})  # 同组另一个人改：以最后一次为准
    mine = room.view(room.a)["course"]["page"]["form"]
    assert mine["done"] and mine["values"]["面积"] == 15.5 and mine["values"]["照片链接"] == "https://example.com/p"
    with pytest.raises(GameError, match="还有组没提交：第2组"):
        room.next()
    table = room.view(teacher)["course"]["page"]["cards"][0]
    assert table["head"][:3] == ["组", "选的地方", "类型"] and table["rows"][0][3] == "15.5平方米" and table["rows"][0][4] == "太暗、没地方坐"
    assert table["rows"][1][:2] == ["第2组", "没有提交"]
    room.clock.forward(20)
    room.next()  # 课后：定题（第 1 次课的最后一个阶段，老师在这里打分）
    room.submit(room.a, {"为谁改": "晚自习后想找地方讨论的同学", "一句话题目": "把楼梯间变成十分钟讨论角"})
    card = room.view(teacher)["course"]["page"]["cards"][0]
    assert card["t"] == "score" and [b["id"] for b in card["blocks"]] == ["B1"] and card["blocks"][0]["deep"] == 1 and card["auto"] == {}
    assert ["现场调研 · 面积", "15.5平方米"] in card["teams"][0]["rows"] and card["teams"][1]["rows"][0] == ["现场调研", "没有提交"]
    with pytest.raises(GameError, match="评分还没打完（还差 2 格）"):
        room.next()
    with pytest.raises(GameError, match="这个板块不属于这次课"):
        room.games.record(room.game, room.ctx(teacher), {"scores": {"第1组": {"B2": 8}}})
    room.games.record(room.game, room.ctx(teacher), {"scores": {"第1组": {"B1": 8.5}, "第2组": {"B1": 4}}})
    state = room.games.state(room.game)
    assert course.card_scores(state, "第1组") == {"K-SJ-001": 8.5} and course.touched_cards(state, "第1组") == ["K-SJ-002"]
    assert course.total(state, "第1组") == (34.0, 0.4)
    assert room.view(room.a)["course"]["page"]["cards"][0]["rows"] == [["发现问题（占总分 40%）", "8.5 分"]]
    room.next()  # 第 2 次课：出方案（必须等倒计时）
    with pytest.raises(GameError, match="倒计时结束前不能提前结束"):
        room.next()
    room.submit(room.a, {"方案说明": "加一盏灯、两条长凳和一块白板。", "预算": 3200})
    past = room.view(room.a)["course"]["pages"]
    assert set(past) == {"n01", "n02", "n03"} and past["n02"]["form"]["values"]["选的地方"] == "二教一楼楼梯间"
    room.clock.forward(30)
    room.next()  # 听使用者怎么说
    spec = course.role("n05", room.games.state(room.game), "第1组")
    assert spec["name"] == "常在这里的同学" and spec["turns"] == 4
    assert "出方案 · 预算：3200元" in spec["prompt"] and "现场调研 · 主要问题：太暗、没地方坐" in spec["prompt"]
    assert "还没有提交任何内容" in course.role("n05", room.games.state(room.game), "第2组")["prompt"]
    seat = room.games.talk_seat(room.game, room.ctx(room.a))
    room.games.talk_save(room.game, room.ctx(room.a), seat.stage, "白板有用吗？", "有用，但晚上得有灯。")
    talk = next(c for c in room.view(room.b)["course"]["page"]["cards"] if c["t"] == "talk")
    assert talk["left"] == 3 and talk["history"] == [{"q": "白板有用吗？", "a": "有用，但晚上得有灯。"}]
    counts = next(c for c in room.view(teacher)["course"]["page"]["cards"] if c["t"] == "kv")
    assert counts["rows"] == [["第1组", "1 次"], ["第2组", "0 次"]] and "晚上得有灯" not in str(room.view(teacher))
    brief = course.brief(room.games.state(room.game), "第1组", "student")
    assert "阶段“听使用者怎么说”" in brief and "要不要改方案（不改、小改、大改）" in brief and "出方案 · 预算：3200元" in brief
    assert "每组最多 4 次" not in brief  # 给老师的话不进学生的助手
    room.submit(room.a, {"要不要改方案": "小改"})
    room.clock.forward(10)
    room.next()  # 总结：第 2 次课的分在这里打
    room.games.record(room.game, room.ctx(teacher), {"scores": {"第1组": {"B2": 9}, "第2组": {"B2": 3}}})
    room.next()
    done = room.view(room.a)
    assert done["finished"] and done["course"]["hint"] == "课程已经全部结束。" and len(done["course"]["pages"]) == 6
    assert course.total(room.games.state(room.game), "第1组") == (88.0, 1.0)
    assert course.card_scores(room.games.state(room.game), "第1组") == {"K-SJ-001": 8.5, "K-SJ-003": 9, "K-SJ-004": 9}


def test_a_trial_of_a_content_course_shows_only_what_was_taught(room):
    game = room.games.create(room.ctx(room.teacher).workspace_id, room.course.ref, seed="t", teams=[{"name": "第1组", "members": [room.a]}], last_lesson=1)
    teacher = room.ctx(room.teacher)
    room.games.advance(game, teacher), room.games.advance(game, teacher)
    room.games.submit(game, room.ctx(room.a), SURVEY)
    room.games.advance(game, teacher)
    room.games.record(game, teacher, {"scores": {"第1组": {"B1": 7}}})
    room.games.advance(game, teacher)
    done = room.games.view(game, room.ctx(room.a))
    assert done["finished"] and done["trial"] == 1 and done["course"]["hint"] == "课程已经全部结束。"
    assert set(done["course"]["pages"]) == {"n01", "n02", "n03"} and set(done["course"]["materials"]) == {"n01", "n02", "n03"}
    assert "听使用者" not in str(done) and "出方案" not in str(done["course"])  # 第 2 次课的内容不带
