"""把标准化对局数据导出为 CSV、Markdown 或 Parquet 宽表。"""

from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from eiketsu_env.config import Settings
from eiketsu_env.db.models import Match, MatchDeck
from eiketsu_env.db.session import make_session_factory
from eiketsu_env.services.card_lookup import load_card_lookup


def _match_rows(settings: Settings) -> list[dict[str, str]]:
    lookup = load_card_lookup(settings)
    factory = make_session_factory(settings)
    rows: list[dict[str, str]] = []
    with factory() as session:
        matches = session.scalars(
            select(Match)
            .options(
                selectinload(Match.sides),
                selectinload(Match.decks).selectinload(MatchDeck.units),
                selectinload(Match.replay_asset),
            )
            .order_by(Match.played_at.desc().nullslast(), Match.id.desc())
        ).all()
        for match in matches:
            deck_hashes = {deck.side_index: [unit.card_hash for unit in deck.units] for deck in match.decks}
            # 规范库保留 hash，导出层再补卡名，避免分析 ID 和人工阅读互相牵制。
            decks = {side: "|".join(hashes) for side, hashes in deck_hashes.items()}
            deck_names = {side: " / ".join(lookup.names(hashes)) for side, hashes in deck_hashes.items()}
            sides = {side.side_index: side for side in match.sides}
            player_name = sides.get(1).player_name if sides.get(1) else ""
            enemy_name = sides.get(2).player_name if sides.get(2) else ""
            summary = f"{match.played_at or '时间不明'} {match.result} {player_name or '-'} vs {enemy_name or '-'}"
            rows.append(
                {
                    "public_id": match.public_id,
                    "replay_id": match.replay_id or "",
                    "detail_t": match.detail_t or "",
                    "played_at": match.played_at or "",
                    "mode": match.mode or "",
                    "version": match.version or "",
                    "result": match.result,
                    "player_name": player_name or "",
                    "enemy_name": enemy_name or "",
                    "summary": summary,
                    "player_deck_names": deck_names.get(1, ""),
                    "enemy_deck_names": deck_names.get(2, ""),
                    "player_deck": decks.get(1, ""),
                    "enemy_deck": decks.get(2, ""),
                    "detail_url": match.detail_url,
                    "play_url": match.play_url or "",
                    "m3u8_url": match.m3u8_url or "",
                    "download_status": match.replay_asset.download_status if match.replay_asset else "",
                }
            )
    return rows


def export_matches(settings: Settings, output_format: str, output: Path | None = None) -> Path:
    rows = _match_rows(settings)
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    if output is None:
        output = settings.exports_dir / f"matches.{output_format}"

    if output_format == "csv":
        fieldnames = list(rows[0].keys()) if rows else [
            "public_id",
            "replay_id",
            "detail_t",
            "played_at",
            "mode",
            "version",
            "result",
            "player_name",
            "enemy_name",
            "summary",
            "player_deck_names",
            "enemy_deck_names",
            "player_deck",
            "enemy_deck",
            "detail_url",
            "play_url",
            "m3u8_url",
            "download_status",
        ]
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return output

    if output_format == "parquet":
        try:
            import pandas as pd
        except ImportError as exc:
            raise RuntimeError("导出 parquet 需要安装：python -m pip install -e .[export]") from exc
        pd.DataFrame(rows).to_parquet(output, index=False)
        return output

    if output_format == "md":
        lines = [
            "# 英杰大战环境对局导出",
            "",
            f"- 样本数：{len(rows)}",
            f"- 卡名映射：{'已加载' if load_card_lookup(settings).cards_by_hash else '未加载'}",
            "- 说明：这里是当前本地库已采集样本，不代表某日全站或关注列表全量。",
            "",
            "| 时间 | 结果 | 主君 | 对手 | 我方卡组 | 对手卡组 | 回放 |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in rows:
            replay = f"[play]({row['play_url']})" if row.get("play_url") else "-"
            lines.append(
                "| {played_at} | {result} | {player} | {enemy} | {player_deck} | {enemy_deck} | {replay} |".format(
                    played_at=row.get("played_at") or "-",
                    result=row.get("result") or "-",
                    player=(row.get("player_name") or "-").replace("|", "/"),
                    enemy=(row.get("enemy_name") or "-").replace("|", "/"),
                    player_deck=(row.get("player_deck_names") or row.get("player_deck") or "-").replace("|", "/"),
                    enemy_deck=(row.get("enemy_deck_names") or row.get("enemy_deck") or "-").replace("|", "/"),
                    replay=replay,
                )
            )
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output

    raise ValueError(f"不支持的导出格式：{output_format}")
