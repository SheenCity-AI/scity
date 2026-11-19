"""课题引擎（通用部分）：一局课、分组、阶段推进、倒计时、各组填报、老师录入。

引擎不懂具体课程的规则，规则在课程插件里（见 course.py）。结果不另存：
任何时候都用全部填报和全部事件重建 GameState，交给插件去算。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .course import GameState, Stage, TeamInfo
from .models import Decision, Game, GameEvent, Team, TeamMember, utcnow
from .spaces import Context, resolve_context


class GameError(Exception):
    """这一步现在不能做，message 是给用户看的原因。"""


@dataclass(frozen=True)
class Seat:
    """一个组此刻和课里的一个 AI 角色对话的现场：角色的设定、已经谈过的、还能问几次。"""

    stage: str
    team: str
    name: str
    prompt: str
    history: tuple[tuple[str, str], ...]
    left: int


def _iso(moment: datetime | None) -> str | None:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


class GameService:
    def __init__(self, sessions: sessionmaker, courses: dict, now: Callable[[], datetime] = utcnow):
        self._sessions = sessions
        self._courses = courses
        self._now = now

    # ---------- 开局 ----------

    def create(self, workspace_id: int, course_ref: str, seed: str, teams: list[dict]) -> int:
        """在一个课题工作区里开一局；这个工作区原来进行中的局归档。"""
        if course_ref not in self._courses:
            raise GameError(f"没有安装课程 {course_ref}")
        members = [user for team in teams for user in team.get("members", [])]
        if len(members) != len(set(members)):
            raise GameError("一个人不能同时在两个组")
        with self._sessions.begin() as db:
            for old in db.scalars(select(Game).where(Game.workspace_id == workspace_id, Game.archived_at.is_(None))):
                old.archived_at = self._now()
            game = Game(workspace_id=workspace_id, course_ref=course_ref, seed=seed)
            db.add(game)
            db.flush()
            for spec in teams:
                team = Team(game_id=game.id, name=spec["name"], company=spec.get("company", ""),
                            npc=spec.get("npc", False))
                db.add(team)
                db.flush()
                for user_id in spec.get("members", []):
                    db.add(TeamMember(game_id=game.id, team_id=team.id, user_id=user_id))
            return game.id

    def active(self, workspace_id: int) -> int | None:
        with self._sessions() as db:
            return db.scalar(select(Game.id).where(Game.workspace_id == workspace_id, Game.archived_at.is_(None)))

    def version(self, game_id: int) -> int:
        with self._sessions() as db:
            return db.get(Game, game_id).version

    # ---------- 内部 ----------

    def _load(self, db: Session, game_id: int, ctx: Context, lock: bool = False) -> tuple[Game, Context]:
        """取出这一局，并按成员关系重新核对身份：只认后台登记的角色。"""
        game = db.scalar(select(Game).where(Game.id == game_id).with_for_update()) if lock else db.get(Game, game_id)
        if game is None:
            raise GameError("没有这一局")
        ctx = resolve_context(db, ctx.user_id, game.workspace_id)
        return game, ctx

    def _mutable(self, db: Session, game_id: int, ctx: Context) -> tuple[Game, Context, object, list[Stage]]:
        game, ctx = self._load(db, game_id, ctx, lock=True)
        if game.archived_at is not None:
            raise GameError("这一局已经归档")
        if game.finished_at is not None:
            raise GameError("这一局已经结束")
        course = self._courses[game.course_ref]
        return game, ctx, course, course.stages

    @staticmethod
    def _stage(stages: list[Stage], key: str | None) -> Stage | None:
        return next((s for s in stages if s.key == key), None)

    def _state(self, db: Session, game: Game) -> GameState:
        teams = db.scalars(select(Team).where(Team.game_id == game.id).order_by(Team.id)).all()
        names = {t.id: t.name for t in teams}
        members: dict[int, list[int]] = {t.id: [] for t in teams}
        for m in db.scalars(select(TeamMember).where(TeamMember.game_id == game.id).order_by(TeamMember.id)):
            members[m.team_id].append(m.user_id)
        elapsed = (self._now() - game.stage_opened_at).total_seconds() / 60 if game.stage_opened_at else 0.0
        state = GameState(seed=game.seed, stage=game.stage, stage_minutes=elapsed,
                          teams=[TeamInfo(t.name, t.company, t.npc, tuple(members[t.id])) for t in teams])
        for d in db.scalars(select(Decision).where(Decision.game_id == game.id).order_by(Decision.id)):
            state.decisions.setdefault(d.stage, {})[names[d.team_id]] = d.payload
            state._history.setdefault((d.stage, names[d.team_id]), []).append(d.payload)
            (state.defaulted.add if d.user_id is None else state.defaulted.discard)((d.stage, names[d.team_id]))
        for e in db.scalars(select(GameEvent).where(GameEvent.game_id == game.id).order_by(GameEvent.id)):
            if e.kind == "result":
                state.results[e.stage] = e.payload
            elif e.kind == "record":
                state.records.setdefault(e.stage, []).append(e.payload)
            elif e.kind == "talk" and e.payload.get("team") in names.values():
                state.talks.setdefault(e.stage, {}).setdefault(e.payload["team"], []).append(
                    {"q": e.payload["q"], "a": e.payload["a"]})
        return state

    @staticmethod
    def _required(course, stage: Stage, state: GameState) -> list[str]:
        """这个阶段要等哪些组提交。默认是全部学生组；课程插件可以用 required_teams 缩小范围。"""
        custom = getattr(course, "required_teams", None)
        return custom(stage.key, state) if custom else [t.name for t in state.teams if not t.npc]

    @staticmethod
    def _require_teacher(ctx: Context) -> None:
        if ctx.role != "teacher":
            raise GameError("只有老师可以做这个操作")

    def _close(self, db: Session, game: Game, course, stage: Stage, ctx: Context) -> None:
        """关闭当前阶段：先检查能不能结束，再给没交的组代填默认选项，最后让课程插件算结果。"""
        now = self._now()
        state = self._state(db, game)
        teams = {t.name: t for t in db.scalars(select(Team).where(Team.game_id == game.id))}
        if stage.form:
            waiting = [t for t in self._required(course, stage, state) if t not in state.decisions.get(stage.key, {})]
            timed_out = game.stage_deadline is not None and now >= game.stage_deadline
            if stage.ends == "timer" and not timed_out:
                raise GameError("这个阶段在倒计时结束前不能提前结束")
            if stage.ends == "timer_or_all" and not timed_out and waiting:
                raise GameError(f"还有组没提交：{'、'.join(waiting)}。等倒计时结束，或全部提交后再结束")
            for team in state.teams:
                if team.name not in state.decisions.get(stage.key, {}):
                    payload = course.default(stage.key, state, team.name)
                    if payload is not None:
                        db.add(Decision(game_id=game.id, team_id=teams[team.name].id, stage=stage.key,
                                        payload=payload, user_id=None))
            db.flush()
            state = self._state(db, game)
        result = course.close(stage.key, state)
        if result is not None:
            db.add(GameEvent(game_id=game.id, kind="result", stage=stage.key, payload=result, user_id=ctx.user_id))

    # ---------- 老师的操作 ----------

    def advance(self, game_id: int, ctx: Context) -> None:
        """结束当前阶段，开启下一个。"""
        with self._sessions.begin() as db:
            game, ctx, course, stages = self._mutable(db, game_id, ctx)
            self._require_teacher(ctx)
            current = self._stage(stages, game.stage)
            if current is not None:
                self._close(db, game, course, current, ctx)
            index = stages.index(current) + 1 if current else 0
            now = self._now()
            if index >= len(stages):
                game.stage, game.stage_opened_at, game.stage_deadline, game.finished_at = None, None, None, now
            else:
                nxt = stages[index]
                game.stage, game.stage_opened_at = nxt.key, now
                game.stage_deadline = now + timedelta(minutes=nxt.minutes) if nxt.minutes else None
                db.add(GameEvent(game_id=game.id, kind="open", stage=nxt.key, payload={}, user_id=ctx.user_id))
            game.version += 1

    def extend(self, game_id: int, ctx: Context, minutes: int) -> None:
        with self._sessions.begin() as db:
            game, ctx, course, stages = self._mutable(db, game_id, ctx)
            self._require_teacher(ctx)
            stage = self._stage(stages, game.stage)
            if stage is None or game.stage_deadline is None:
                raise GameError("当前阶段没有倒计时")
            used = sum(e.payload["minutes"] for e in db.scalars(select(GameEvent).where(
                GameEvent.game_id == game.id, GameEvent.kind == "extend", GameEvent.stage == stage.key)))
            if minutes <= 0 or used + minutes > stage.extend_minutes:
                raise GameError(f"这个阶段最多还能延长 {stage.extend_minutes - used} 分钟")
            game.stage_deadline += timedelta(minutes=minutes)
            db.add(GameEvent(game_id=game.id, kind="extend", stage=stage.key, payload={"minutes": minutes},
                             user_id=ctx.user_id))
            game.version += 1

    def record(self, game_id: int, ctx: Context, payload: dict) -> None:
        """老师录入现场发生的事（如举牌结果）。录错了再录一次，由课程插件决定怎么取舍。"""
        with self._sessions.begin() as db:
            game, ctx, course, stages = self._mutable(db, game_id, ctx)
            self._require_teacher(ctx)
            if game.stage is None:
                raise GameError("这一局还没开始")
            try:
                payload = course.validate_record(game.stage, payload, self._state(db, game))
            except ValueError as exc:
                raise GameError(str(exc)) from exc
            db.add(GameEvent(game_id=game.id, kind="record", stage=game.stage, payload=payload, user_id=ctx.user_id))
            game.version += 1

    # ---------- 学生的操作 ----------

    def submit(self, game_id: int, ctx: Context, payload: dict) -> None:
        with self._sessions.begin() as db:
            game, ctx, course, stages = self._mutable(db, game_id, ctx)
            member = db.scalar(select(TeamMember).where(TeamMember.game_id == game.id,
                                                        TeamMember.user_id == ctx.user_id))
            if member is None:
                raise GameError("你不在这一局的任何组里")
            stage = self._stage(stages, game.stage)
            if stage is None or not stage.form:
                raise GameError("当前阶段不需要填报")
            if game.stage_deadline is not None and self._now() >= game.stage_deadline:
                raise GameError("这个阶段的填报已经截止")
            team = db.get(Team, member.team_id)
            try:
                payload = course.validate(stage.key, payload, self._state(db, game), team.name)
            except ValueError as exc:
                raise GameError(str(exc)) from exc
            db.add(Decision(game_id=game.id, team_id=team.id, stage=stage.key, payload=payload, user_id=ctx.user_id))
            game.version += 1

    # ---------- 和课里的 AI 角色对话 ----------

    def _seat(self, db: Session, game: Game, ctx: Context, course) -> Seat:
        member = db.scalar(select(TeamMember).where(TeamMember.game_id == game.id, TeamMember.user_id == ctx.user_id))
        if member is None:
            raise GameError("你不在这一局的任何组里")
        team = db.get(Team, member.team_id).name
        state = self._state(db, game)
        make = getattr(course, "role", None)
        spec = make(game.stage, state, team) if make and game.stage else None
        if not spec:
            raise GameError("这个阶段没有可以对话的角色")
        if game.stage_deadline is not None and self._now() >= game.stage_deadline:
            raise GameError("这个阶段已经截止")
        asked = state.talks.get(game.stage, {}).get(team, [])
        return Seat(game.stage, team, spec["name"], spec["prompt"], tuple((t["q"], t["a"]) for t in asked),
                    max(0, int(spec["turns"]) - len(asked)))

    def talk_seat(self, game_id: int, ctx: Context) -> Seat:
        """开口之前：取出角色设定和已谈过的内容。次数用完、阶段截止、不在组里都会拒绝。"""
        with self._sessions() as db:
            game, ctx = self._load(db, game_id, ctx)
            if game.archived_at is not None or game.finished_at is not None:
                raise GameError("这一局已经结束")
            seat = self._seat(db, game, ctx, self._courses[game.course_ref])
        if seat.left <= 0:
            raise GameError(f"和{seat.name}的对话次数已经用完")
        return seat

    def talk_save(self, game_id: int, ctx: Context, stage: str, question: str, answer: str) -> int:
        """记下一问一答，返回这个组还能问几次。模型回答期间阶段已经换了，就不记。"""
        with self._sessions.begin() as db:
            game, ctx, course, _ = self._mutable(db, game_id, ctx)
            if game.stage != stage:
                raise GameError("这个阶段已经结束")
            seat = self._seat(db, game, ctx, course)
            if seat.left <= 0:
                raise GameError(f"和{seat.name}的对话次数已经用完")
            db.add(GameEvent(game_id=game.id, kind="talk", stage=stage, user_id=ctx.user_id,
                             payload={"team": seat.team, "q": question, "a": answer}))
            game.version += 1
            return seat.left - 1

    # ---------- 查看 ----------

    def brief(self, ctx: Context) -> str:
        """这个人所在工作区里正在进行的一局，现在是什么情况。给助手看，让它知道课上到哪了、这个人看到了什么。
        没有进行中的一局，或者课程没有提供摘要，返回空字符串。"""
        if ctx.workspace_id is None:
            return ""
        game_id = self.active(ctx.workspace_id)
        if game_id is None:
            return ""
        with self._sessions() as db:
            game, ctx = self._load(db, game_id, ctx)
            course = self._courses.get(game.course_ref)
            make = getattr(course, "brief", None)
            if make is None:
                return ""
            state = self._state(db, game)
            team = next((t.name for t in state.teams if ctx.user_id in t.members), None)
            return make(state, team, ctx.role) or ""

    def state(self, game_id: int) -> GameState:
        with self._sessions() as db:
            return self._state(db, db.get(Game, game_id))

    def view(self, game_id: int, ctx: Context) -> dict:
        """这个人此刻看到的一局：通用部分由引擎给，课程内容由插件按身份给。"""
        with self._sessions() as db:
            game, ctx = self._load(db, game_id, ctx)
            course = self._courses[game.course_ref]
            state = self._state(db, game)
            team = next((t.name for t in state.teams if ctx.user_id in t.members), None)
            current = self._stage(course.stages, game.stage)
            position = course.stages.index(current) if current else (len(course.stages) if game.finished_at else -1)
            view = {
                "game": game.id, "version": game.version, "role": ctx.role, "team": team,
                "now": _iso(self._now()), "finished": game.finished_at is not None,
                "archived": game.archived_at is not None,
                "stage": current and {"key": current.key, "name": current.name, "lesson": current.lesson,
                                      "form": current.form, "ends": current.ends,
                                      "opened_at": _iso(game.stage_opened_at), "deadline": _iso(game.stage_deadline)},
                "stages": [{"key": s.key, "name": s.name, "lesson": s.lesson,
                            "status": "done" if i < position else "current" if i == position else "todo"}
                           for i, s in enumerate(course.stages)],
                "course": course.view(state, team, ctx.role),
            }
            if ctx.role == "teacher" and current and current.form:
                done = state.decisions.get(current.key, {})
                view["submitted"] = {t: t in done for t in self._required(course, current, state)}
            return view
