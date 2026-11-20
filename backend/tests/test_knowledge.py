"""知识图谱的总图：从表格导入专业目录、知识领域、知识点、相近专业；可以反复导入；按专业分圈。"""
from pathlib import Path

import pytest

from scity.knowledge import KnowledgeMap

BOM = "\ufeff"


def write(folder: Path, name: str, text: str) -> None:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BOM + text, encoding="utf-8")


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "总图"
    write(f, "专业目录/本科专业目录-2026.csv", "门类代码,学科门类,专业类代码,专业类,专业代码,专业名称,备注\n"
          "08,工学,0828,建筑类,082801,建筑学,\n08,工学,0828,建筑类,082802,城乡规划,\n12,管理学,1201,管理科学与工程类,120104,房地产开发与管理,\n"
          "01,哲学,0101,哲学类,010101,哲学,\n")
    write(f, "专业目录/职业教育专业目录-2021年版.csv", "层次,专业大类代码,专业大类,专业类代码,专业类,专业代码,专业名称,来源\n"
          "中职,64,土木建筑大类,6401,建筑设计类,640101,建筑表现,2021年版\n高职专科,44,土木建筑大类,4401,建筑设计类,440101,建筑设计,2021年版\n"
          "职业本科,24,土木建筑大类,2401,建筑设计类,240101,建筑设计,2021年版\n")
    write(f, "知识领域v0.1.csv", "大领域代码,大领域,知识领域编号,知识领域,适用范围,现有知识点数,来源,备注\n"
          "KF,城市开发与更新,KF.2,融资拿地,建筑相关,2,,\nKF,城市开发与更新,KF.3,方案前期,建筑相关,1,,\nSJ,设计基础,SJ.1,空间与尺度,建筑相关,0,,\n")
    write(f, "知识点清单v0.1.csv", "编号,名称,知识领域编号,关键词,相关知识点,本科专业代码,高职专业代码,来源课程包,核对状态,一句话\n"
          "K-KF-004,土地所有权与使用权,KF.2,出让年限、土地使用权,K-KF-008,120104,,沙盘,查到了出处,买到的是使用权\n"
          "K-KF-008,规划条件指标,KF.2,容积率、绿地率、限高,K-KF-004、K-KF-021,082801、082802,440101、240101,沙盘,,这块地能盖多少\n"
          "K-KF-021,可行性研究,KF.3,可研,K-KF-999,082801、999999,,沙盘,,先算账\nK-XX-001,没有领域的,XX.9,,,,,,,\n")
    write(f, "相近专业对照表v0.1.csv", "专业代码,专业名称,目录,相近专业代码,相近专业名称,相近专业目录,依据,专业群,相近专业所在专业群\n"
          "082801,建筑学,本科,082802,城乡规划,本科,同一专业群,A 建筑设计,A 建筑设计\n082802,城乡规划,本科,082801,建筑学,本科,同一专业群,A 建筑设计,A 建筑设计\n"
          "082801,建筑学,本科,440101,建筑设计,高职专科,同一专业群,A 建筑设计,A 建筑设计\n082801,建筑学,本科,888888,不存在,本科,,,\n")
    return f


def test_import_builds_the_map_and_reports_what_does_not_line_up(sessions, folder):
    km = KnowledgeMap(sessions)
    report = km.import_dir(folder)
    assert (report["majors"], report["domains"], report["points"]) == (6, 3, 3)  # 中职不收；领域对不上的知识点不收
    assert (report["keywords"], report["links"], report["point_majors"], report["neighbors"]) == (6, 3, 6, 3)
    assert km.stats() == {"majors": {"本科": 4, "高职专科": 1, "职业本科": 1}, "domains": 3, "groups": 2, "points": 3, "keywords": 6,
                          "links": 3, "point_majors": 6, "neighbors": 3}
    text = "\n".join(report["warnings"])
    assert "K-XX-001 的知识领域 XX.9 不在知识领域表里" in text and "K-KF-021 的相关知识点 K-KF-999 不在清单里" in text
    assert "K-KF-021 挂的专业代码 999999 不在专业目录里" in text and "888888 有一边不在专业目录里" in text
    point = km.point("K-KF-008")
    assert point["name"] == "规划条件指标" and point["domain"] == "融资拿地" and (point["group_code"], point["group"]) == ("KF", "城市开发与更新")
    assert point["keywords"] == ["容积率", "绿地率", "限高"] and point["related"] == ["K-KF-004", "K-KF-021"]
    assert km.match("容积率") == ["K-KF-008"] and km.match("可行性研究") == ["K-KF-021"] and km.match("辩论") == [] and km.point("K-KF-999") is None


def test_rings_follow_the_students_own_major(sessions, folder):
    km = KnowledgeMap(sessions)
    km.import_dir(folder)
    arch, plan, estate, phil = (km.find_major("本科", c) for c in ("082801", "082802", "120104", "010101"))
    assert km.near(arch) == {plan, km.find_major("高职专科", "440101")} and km.near(estate) == set()
    assert km.ring(arch, "K-KF-008") == 1     # 本专业的知识点
    assert km.ring(plan, "K-KF-021") == 1     # 相近专业（建筑学）的知识点
    assert km.ring(arch, "K-KF-004") == 2     # 只挂在房地产专业：对建筑学学生是第二圈
    assert km.ring(estate, "K-KF-004") == 1 and km.ring(phil, "K-KF-008") == 2 and km.ring(None, "K-KF-008") == 2
    assert km.find_major("中职", "640101") is None and km.find_major("本科", "440101") is None


def test_importing_again_updates_in_place_and_keeps_majors_that_left_the_list(sessions, folder):
    km = KnowledgeMap(sessions)
    km.import_dir(folder)
    arch = km.find_major("本科", "082801")
    write(folder, "专业目录/本科专业目录-2026.csv", "门类代码,学科门类,专业类代码,专业类,专业代码,专业名称,备注\n08,工学,0828,建筑类,082801,建筑学（新名）,\n")
    write(folder, "知识点清单v0.2.csv", "编号,名称,知识领域编号,关键词,相关知识点,本科专业代码,高职专业代码,来源课程包,核对状态,一句话\n"
          "K-KF-008,规划条件指标,KF.3,容积率,,082801,,沙盘,,改了领域\n")
    report = km.import_dir(folder)
    assert km.find_major("本科", "082801") == arch and km.stats()["majors"]["本科"] == 4  # 编号不变；不在新目录里的专业没有删
    assert any("3 个专业不在这次的目录里" in w for w in report["warnings"])
    point = km.point("K-KF-008")
    assert point["domain"] == "方案前期" and point["keywords"] == ["容积率"] and point["related"] == [] and point["summary"] == "改了领域"
    assert km.stats()["points"] == 3 and km.match("绿地率") == []  # 清单里没有的知识点留着；这个知识点的关键词按新表


def test_a_student_registers_a_major_and_the_map_sorts_points_into_rings(sessions, folder, spaces, wallets):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from scity.admin import open_course
    from scity.models import Game, KnowledgeMark, Workspace
    from scity.web import create_app, sign_session
    from tests.test_web import SECRET, FakeAssistant, FakeLogin
    km = KnowledgeMap(sessions)
    km.import_dir(folder)
    roster = {"school": "甲学校", "class": "一班", "course": {"name": "沙盘", "ref": "toy@1"},
              "students": [{"open_id": "ou_a", "name": "甲", "level": "本科", "major_code": "082802"}, {"open_id": "ou_b", "name": "乙"}]}
    with pytest.raises(ValueError, match="专业目录里没有“本科 999999”"):  # 代码写错：整份名单拒绝，不留下建了一半的班
        open_course(sessions, spaces, wallets, {**roster, "students": [{"open_id": "ou_x", "name": "丙", "level": "本科", "major_code": "999999"}]})
    assert spaces.find_space("school", "甲学校") is None
    opened = open_course(sessions, spaces, wallets, roster)
    a, b = spaces.ensure_user("ou_a"), spaces.ensure_user("ou_b")
    assert km.major_of(a)["name"] == "城乡规划" and km.major_of(b) is None  # 名单里带了专业的，开班时就登记上
    with sessions.begin() as db:
        game = Game(workspace_id=opened.workspace_id, course_ref="toy@1")
        db.add(game)
        db.flush()
        for user in (a, b):
            for code in ("K-KF-004", "K-KF-008"):
                db.add(KnowledgeMark(user_id=user, point_code=code, game_id=game.id, course_ref="toy@1", score=7))
    # K-KF-008 挂在建筑学、城乡规划下面；K-KF-004 只挂在房地产开发与管理下面
    assert {x["code"]: x["ring"] for x in km.mine(a)["points"]} == {"K-KF-004": 2, "K-KF-008": 1}
    assert km.mine(a)["major"]["level"] == "本科" and km.mine(a)["rings"] == {1: "本专业和相近专业", 2: "其他专业", 3: "专业以外的兴趣"}
    assert {x["ring"] for x in km.mine(b)["points"]} == {None} and km.mine(b)["major"] is None  # 没登记专业：先不分圈
    settings = SimpleNamespace(spend_cap_yuan=None, demo_enroll=False, web_secret=SECRET, feishu_app_id="cli_test")
    client = TestClient(create_app(settings, sessions, spaces, wallets, FakeAssistant(), FakeLogin()), base_url="https://testserver")
    assert client.get("/api/majors", params={"level": "本科", "q": "建筑"}).status_code == 401
    client.cookies.set("scity_session", sign_session(b, SECRET))
    found = client.get("/api/majors", params={"level": "本科", "q": "建筑"}).json()["majors"]
    assert [m["name"] for m in found] == ["建筑学", "城乡规划"] and found[0]["class_name"] == "建筑类"  # 按名称和专业类都能找到
    assert [m["name"] for m in client.get("/api/majors", params={"level": "高职专科", "q": "4401"}).json()["majors"]] == ["建筑设计"]
    assert client.get("/api/majors", params={"level": "中职", "q": ""}).status_code == 422
    assert client.post("/api/me/major", json={"major_id": 99999}).status_code == 404
    done = client.post("/api/me/major", json={"major_id": found[0]["id"]}).json()
    assert done["major"]["name"] == "建筑学" and client.get("/api/knowledge/me").json()["major"]["code"] == "082801"
    assert {x["code"]: x["ring"] for x in client.get("/api/knowledge/me").json()["points"]} == {"K-KF-004": 2, "K-KF-008": 1}
    assert km.major_of(a)["name"] == "城乡规划"  # 只改了自己的


def test_demo_map_is_built_from_the_real_catalogue_and_marked_as_demo(sessions, folder):
    km = KnowledgeMap(sessions)
    km.import_dir(folder)
    demo = km.demo()
    assert demo["demo"] is True and demo["major"]["name"] == "建筑学" and demo == km.demo()  # 每次都一样
    real = [x for x in demo["points"] if not x["derived"]]
    assert {x["code"] for x in real} == {"K-KF-004", "K-KF-008", "K-KF-021"} and all(x["in_map"] and x["courses"] == ["演示课程"] for x in real)
    assert {x["code"]: x["ring"] for x in real}["K-KF-004"] == 2 and {x["code"]: x["ring"] for x in real}["K-KF-008"] == 1  # 分圈按真实的专业对照
    derived = [x for x in demo["points"] if x["derived"]]
    assert len(derived) == 9 and {x["ring"] for x in derived} == {1, 2, 3} and all(x["score"] is None and x["code"].startswith("DEMO-") for x in derived)
    assert sum(demo["counts"].values()) == len(real)  # 四档的数量只数课程里的（实线）
    assert all(x["derived"] is False for x in km.mine(1)["points"]) and "demo" not in km.mine(1)
