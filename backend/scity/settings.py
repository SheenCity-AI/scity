"""运行配置：从环境变量和 SCITY_ENV_FILES 指定的文件读取，代码里不放任何密钥。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILES = "~/.config/scity/ark.env:~/.config/scity/feishu-dev.env"
DEFAULT_MODEL = "doubao-seed-2-1-turbo-260628"

# 演示用的换算，不代表真实定价；真实价格放私有目录的 pricing.json。
DEMO_PRICING = {"default": {"input_per_1k": 1.0, "output_per_1k": 3.0}}

DEFAULT_PROMPT = (
    "你是 SCity 助手，陪学生学习的 AI 助理。回答简洁、直接，先给结论。"
    "学生问自己的点数时，调用工具查询，不要猜。"
)


def _read_env_files(paths: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in paths.split(":"):
        path = Path(raw).expanduser()
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values


@dataclass(frozen=True)
class Settings:
    ark_api_key: str
    ark_base_url: str
    feishu_app_id: str
    feishu_app_secret: str
    database_url: str
    session_database_url: str
    private_dir: Path
    default_model: str
    pricing: dict = field(default_factory=dict)

    def prompt(self, name: str, default: str) -> str:
        path = self.private_dir / "prompts" / f"{name}.md"
        return path.read_text(encoding="utf-8").strip() if path.is_file() else default


def load_settings() -> Settings:
    env = {**_read_env_files(os.environ.get("SCITY_ENV_FILES", DEFAULT_ENV_FILES)), **os.environ}
    data_dir = Path(env.get("SCITY_DATA_DIR", BACKEND_DIR / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    private_dir = Path(env.get("SCITY_PRIVATE_DIR", BACKEND_DIR.parent.parent / "scity-private"))
    pricing_file = private_dir / "pricing.json"
    pricing = json.loads(pricing_file.read_text(encoding="utf-8")) if pricing_file.is_file() else DEMO_PRICING
    return Settings(
        ark_api_key=env.get("ARK_API_KEY", ""),
        ark_base_url=env.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
        feishu_app_id=env.get("FEISHU_APP_ID", ""),
        feishu_app_secret=env.get("FEISHU_APP_SECRET", ""),
        database_url=env.get("SCITY_DATABASE_URL", f"sqlite:///{data_dir}/scity.db"),
        session_database_url=env.get("SCITY_SESSION_DATABASE_URL", f"sqlite+aiosqlite:///{data_dir}/sessions.db"),
        private_dir=private_dir,
        default_model=env.get("SCITY_MODEL", DEFAULT_MODEL),
        pricing=pricing,
    )
