from __future__ import annotations

import json
import subprocess
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from eiketsu_env.cli import build_parser
from eiketsu_env.config import Settings
from eiketsu_env.db.base import Base
from eiketsu_env.db.models import Match, SharedContributionMatch, SharedContributionPackage
from eiketsu_env.db.session import make_engine
from eiketsu_env.services.collector import CollectResult
from eiketsu_env.services.repository import EnvRepository
from eiketsu_env.services.share import ShareConfig, export_contribution, import_contributions, load_share_config, sync_shared


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        db_url=f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}",
        firefox_profile=tmp_path / "ff",
        card_catalog_path=tmp_path / "cards.json",
    )


def _config() -> ShareConfig:
    return ShareConfig(target_version="Ver.share", date_from="2026-05-10", date_to="2026-05-12")


def _detail(replay_id: str, version: str = "Ver.share", result: str = "win") -> dict:
    return {
        "detail_url": "https://eiketsu-taisen.net/members/history/detail?f=586",
        "url": "https://eiketsu-taisen.net/members/history/detail?f=586",
        "follow_id": "586",
        "played_at": "2026-05-11 12:34",
        "date": "2026-05-11 12:34",
        "mode": "全国対戦",
        "version": version,
        "result": result,
        "replay_id": replay_id,
        "play_url": f"https://eiketsu-taisen.net/members/enbujyo/play?p={replay_id}",
        "m3u8_url": f"https://dl.eiketsu-taisen.net/live/{replay_id}/master.m3u8",
        "castle_breakdown": {"rows": [{"player": "20.00%", "enemy": "100.00%"}]},
        "timeline_labels": ["開幕", "終了"],
        "timeline_data": {"castle": {"player": [100, 80], "enemy": [100, 0]}},
        "players": [
            {
                "side_index": 1,
                "role": "player",
                "player_name": "A",
                "follow_id": "586",
                "result": result,
                "castle_rate": "80.00%",
                "deck_ids": ["card-a", "card-b"],
                "profile": {"全国主君ランキング": "50 位"},
            },
            {
                "side_index": 2,
                "role": "enemy",
                "player_name": "B",
                "result": "unknown",
                "castle_rate": "0.00%",
                "deck_ids": ["card-c"],
                "profile": {"全国主君ランキング": "120 位"},
            },
        ],
    }


def _init_db(settings: Settings):
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    return engine


def test_export_contribution_jsonl_keeps_only_standardized_data(tmp_path):
    settings = _settings(tmp_path)
    engine = _init_db(settings)
    with Session(engine) as session:
        repo = EnvRepository(session, settings)
        repo.upsert_match_detail(_detail("replay-share"))
        repo.upsert_match_detail(_detail("replay-old", version="Ver.old"))
        session.commit()

    result = export_contribution(settings, _config(), "alice")
    lines = [json.loads(line) for line in result.path.read_text(encoding="utf-8").splitlines()]

    assert lines[0]["record_type"] == "manifest"
    assert lines[0]["schema_version"] == "share_v1"
    assert lines[0]["match_count"] == 1
    assert result.match_count == 1

    record = lines[1]
    assert record["record_type"] == "match"
    assert record["version"] == "Ver.share"
    assert record["players"][0]["deck_ids"] == ["card-a", "card-b"]
    serialized = json.dumps(record, ensure_ascii=False)
    assert "local_path" not in serialized
    assert "raw_snapshots" not in serialized
    assert "cookies" not in serialized
    assert "firefox_profile" not in serialized


def test_import_contributions_is_idempotent_and_dedupes_replay(tmp_path):
    source_settings = _settings(tmp_path / "source")
    source_engine = _init_db(source_settings)
    with Session(source_engine) as session:
        repo = EnvRepository(session, source_settings)
        repo.upsert_match_detail(_detail("same-replay"))
        session.commit()
    alice_package = export_contribution(source_settings, _config(), "alice")
    bob_package = export_contribution(source_settings, _config(), "bob")

    dest_settings = _settings(tmp_path / "dest")
    dest_engine = _init_db(dest_settings)

    first = import_contributions(dest_settings, [alice_package.path])
    second = import_contributions(dest_settings, [alice_package.path])
    third = import_contributions(dest_settings, [bob_package.path])

    assert first.packages_imported == 1
    assert first.matches_imported == 1
    assert second.packages_skipped == 1
    assert second.matches_imported == 0
    assert third.packages_imported == 1

    with Session(dest_engine) as session:
        assert len(session.scalars(select(Match)).all()) == 1
        assert len(session.scalars(select(SharedContributionPackage)).all()) == 2
        assert len(session.scalars(select(SharedContributionMatch)).all()) == 2


def test_load_share_config_extends_stale_date_to_for_tools(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "share_config.json").write_text(
        json.dumps(
            {
                "schema_version": "share_v1",
                "target_version": "Ver.share",
                "date_from": "2026-05-10",
                "date_to": "2026-05-12",
                "include_solo": False,
                "high_ranker_rank": 100,
                "report_formats": ["md"],
                "reports": ["overview"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    from eiketsu_env.services import share as share_service

    monkeypatch.setattr(share_service, "_latest_collectable_game_date", lambda today=None: "2026-05-20")

    raw = load_share_config(settings, effective=False)
    effective = load_share_config(settings)

    assert raw.date_to == "2026-05-12"
    assert effective.date_to == "2026-05-20"


def test_sync_uses_only_shared_git_paths_and_retries_push(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    engine = _init_db(settings)
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "share_config.json").write_text(
        json.dumps(
            {
                "schema_version": "share_v1",
                "target_version": "Ver.share",
                "date_from": "2026-05-10",
                "date_to": "2026-05-12",
                "include_solo": False,
                "high_ranker_rank": 100,
                "report_formats": ["md"],
                "reports": ["overview"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with Session(engine) as session:
        repo = EnvRepository(session, settings)
        repo.upsert_match_detail(_detail("sync-replay"))
        session.commit()

    from eiketsu_env.services import share as share_service

    monkeypatch.setattr(share_service, "upgrade_database", lambda _settings: None)
    runner = _FakeGitRunner()

    result = sync_shared(settings, "alice", skip_collect=True, runner=runner)

    assert result.committed is True
    assert result.pushed is True
    add_calls = [call for call in runner.calls if call[:3] == ["git", "add", "--"]]
    assert add_calls
    assert all(not part.startswith("data") for call in add_calls for part in call[3:])
    assert runner.calls.count(["git", "pull", "--rebase"]) == 2
    assert runner.calls.count(["git", "push"]) == 2


def test_sync_shared_collects_without_raw_snapshots(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    engine = _init_db(settings)
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "share_config.json").write_text(
        json.dumps(
            {
                "schema_version": "share_v1",
                "target_version": "Ver.share",
                "date_from": "2026-05-10",
                "date_to": "2026-05-12",
                "include_solo": False,
                "high_ranker_rank": 100,
                "report_formats": ["md"],
                "reports": ["overview"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with Session(engine) as session:
        repo = EnvRepository(session, settings)
        repo.upsert_match_detail(_detail("sync-no-raw"))
        session.commit()

    from eiketsu_env.services import share as share_service

    seen_kwargs: dict = {}

    def fake_collect(*args, **kwargs):
        seen_kwargs.update(kwargs)
        return CollectResult(1, "completed", {"matches": 1}, [])

    monkeypatch.setattr(share_service, "upgrade_database", lambda _settings: None)
    monkeypatch.setattr(share_service, "collect_follow", fake_collect)

    sync_shared(settings, "alice", runner=_FakeGitRunner())

    assert seen_kwargs["save_raw_snapshots"] is False


def test_share_cli_parser_accepts_sync_command():
    args = build_parser().parse_args(["share", "sync", "--contributor", "alice", "--skip-collect"])

    assert args.command == "share"
    assert args.share_command == "sync"
    assert args.contributor == "alice"
    assert args.skip_collect is True


class _FakeGitRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.push_count = 0

    def __call__(self, args, cwd: Path):
        command = list(args)
        self.calls.append(command)
        if command == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(command, 0, "true\n", "")
        if command == ["git", "diff", "--cached", "--name-only"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command == ["git", "diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(command, 1, "", "")
        if command == ["git", "push"]:
            self.push_count += 1
            if self.push_count == 1:
                return subprocess.CompletedProcess(command, 1, "", "rejected")
        return subprocess.CompletedProcess(command, 0, "", "")
