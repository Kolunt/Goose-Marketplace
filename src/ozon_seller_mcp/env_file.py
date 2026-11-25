"""Загрузка .env из корня проекта (не перезаписывает уже заданные переменные)."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    here = Path(__file__).resolve().parent
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()


def _apply_dotenv_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        existing = os.environ.get(key)
        if existing is not None and existing.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value


def dotenv_candidates() -> list[Path]:
    """Только явный путь и .env в корне репозитория (не C:\\Users\\…\\.env при cwd дома)."""
    paths: list[Path] = []
    custom = os.environ.get("OZON_SELLER_ENV_FILE", "").strip()
    if custom:
        paths.append(Path(custom))
    paths.append(project_root() / ".env")
    return paths


def load_project_dotenv() -> list[Path]:
    """Подставить в os.environ пары из .env.

    Переменные Goose/оболочки с непустым значением не трогаем. Пустые ключи
    (часто UI расширения задаёт имя без значения) можно заполнить из .env.
    """
    loaded: list[Path] = []
    for path in dotenv_candidates():
        if not path.is_file():
            continue
        _apply_dotenv_file(path)
        loaded.append(path.resolve())
    return loaded
