from __future__ import annotations

from eiketsu_env.services.mode_filter import is_environment_mode
from eiketsu_env.services.parsers import parse_daily_html, parse_detail_html, parse_follow_api_json, parse_follow_html, parse_replay_html


FOLLOW_HTML = """
<ul>
  <li class="c-name-plate__wrap">
    <a href="/members/history/daily?f=586">history</a>
    <span class="c-name-plate__name-text">最強の初心者</span>
    <span class="c-name-plate__state-text">全国</span>
  </li>
</ul>
"""


DAILY_HTML = """
<section class="p-daily-record">
  <a class="p-daily-log__detail-nav" href="/members/history/detail?t=1773932045&f=586">
    <div class="c-mode-time win">
      <span class="c-mode-time__label">全国対戦</span>
      <span class="c-mode-time__label">23:54</span>
    </div>
    <span class="c-match-up__name-box">最強の初心者</span>
    <span class="c-match-up__name-box">どんべえ</span>
    <span class="c-v2-castle-data__wrap">82.00%</span>
    <span class="c-v2-castle-data__wrap">0.00%</span>
  </a>
</section>
"""


DETAIL_HTML = """
<div id="detail_head" class="win">
  <div class="player"><div class="title"><img src="https://image.eiketsu-taisen.net/league_v3/a.png" alt="旅人"></div><span class="name"><span class="etfont">最強の初心者</span></span></div>
  <div class="enemy"><div class="title"><img src="https://image.eiketsu-taisen.net/league_v3/b.png" alt="騎士1"></div><span class="name"><span class="etfont">どんべえ</span></span></div>
</div>
<div class="trapezoid"><span class="inner">全国対戦 Ver.3.1.0H</span></div>
<a href="/members/enbujyo/play?p=b925ff0584d242fa895d47e03306a4b8">play</a>
<section class="p-detail--player">
  <div class="p-detail__score-box"><dt>証</dt><dd>十二</dd></div>
  <div class="c-selected__container">
    <span class="etfont">Weapon Test</span>
    <p class="c-selected__explain c-selected__explain--buff">
      <span class="c-sec"><b class="c-sec__text">Soul A</b></span>
      <span class="c-sec"><b class="c-sec__text">Soul B</b></span>
    </p>
  </div>
  <div class="c-selected__container">
    <span class="etfont">Style Test</span>
  </div>
</section>
<section class="p-detail--enemy"></section>
<section class="p-general-score">
  <a onclick="overwriteCheck('hash-a,hash-b')"></a>
  <table>
    <tr class="p-gen-tbl__row"><th>&#x6483;&#x7834;</th><td class="p-gen-tbl__cel--score">2</td><td class="p-gen-tbl__cel--score">3</td></tr>
    <tr class="p-gen-tbl__row"><th>&#x64A4;&#x9000;</th><td class="p-gen-tbl__cel--score">1</td><td class="p-gen-tbl__cel--score">4</td></tr>
  </table>
</section>
<section class="p-general-score">
  <a onclick="overwriteCheck('hash-c,hash-d,hash-e')"></a>
  <table>
    <tr class="p-gen-tbl__row"><th>&#x6483;&#x7834;</th><td class="p-gen-tbl__cel--score">6</td></tr>
    <tr class="p-gen-tbl__row"><th>&#x64A4;&#x9000;</th><td class="p-gen-tbl__cel--score">2</td></tr>
  </table>
</section>
<section class="castle">
  <div class="trapezoid"><span class="inner">城ゲージ</span></div>
  <ul class="castle_damage"><li class="player">82.00%</li><li class="mincho">残城</li><li class="enemy">0.00%</li></ul>
</section>
<section class="timelines"><h3 class="timeline-title">流派</h3></section>
<script>
var p_schoolCnt = [50, 30, -1];
var e_schoolCnt = [45, 20, -1];
var p_waveCnt = [70];
var e_waveCnt = [60];
var p_castleTL = [10000, 8200];
var e_castleTL = [10000, 0];
</script>
"""


REPLAY_HTML = """
<ul>
  <li class="c-name-plate__wrap">
    <a href="/members/history/daily?f=586">history</a>
    <span class="c-name-plate__name-text">Player A</span>
  </li>
  <li class="c-name-plate__wrap">
    <a data-code="opponent-code">follow</a>
    <span class="c-name-plate__name-text">Opponent B</span>
  </li>
</ul>
<button data-p="b925ff0584d242fa895d47e03306a4b8"></button>
<script>const u = "https://dl.eiketsu-taisen.net/live/b925ff0584d242fa895d47e03306a4b8/master.m3u8";</script>
<section class="general_score player1"><a onclick="overwriteCheck('hash-a,hash-b')"></a></section>
<section class="general_score player2"><a onclick="overwriteCheck('hash-c')"></a></section>
"""


def test_parse_follow_html_discovers_players():
    players = parse_follow_html(FOLLOW_HTML, "https://eiketsu-taisen.net/members/follow/", "https://eiketsu-taisen.net")

    assert players == [
        {
            "follow_id": "586",
            "name": "最強の初心者",
            "state": "全国",
            "daily_url": "https://eiketsu-taisen.net/members/history/daily?f=586",
        }
    ]


def test_parse_daily_html_extracts_detail_seeds():
    seeds = parse_daily_html(
        DAILY_HTML,
        "https://eiketsu-taisen.net/members/history/daily?f=586",
        "https://eiketsu-taisen.net",
        "2026-03-20",
        {"follow_id": "586", "name": "最強の初心者"},
    )

    assert len(seeds) == 1
    assert seeds[0]["detail_url"] == "https://eiketsu-taisen.net/members/history/detail?t=1773932045&f=586"
    assert seeds[0]["played_at"] == "2026-03-20 23:54"
    assert seeds[0]["result"] == "win"
    assert seeds[0]["opponent_name"] == "どんべえ"


def test_parse_daily_html_skips_when_page_date_differs_from_requested_date():
    html = DAILY_HTML.replace(
        '<section class="p-daily-record">',
        '<section class="p-daily-record"><dt class="c-btl-record__rec-ttl--total">2026/3/19</dt>',
    )

    seeds = parse_daily_html(
        html,
        "https://eiketsu-taisen.net/members/history/daily?y=2026&m=3&d=20&f=586",
        "https://eiketsu-taisen.net",
        "2026-03-20",
        {"follow_id": "586", "name": "æœ€å¼·ã®åˆå¿ƒè€…"},
    )

    assert seeds == []


def test_parse_follow_api_json_extracts_real_follows_only():
    payload = {
        "follow": [
            "ルルルカ,食客,img,t0,img,,,,,,,,,,,,,,,,,,,,,,,2,425521,code,1759109975,1778463427,6537,1,1,store,0",
            "申請中,食客,img,t0,img,,,,,,,,,,,,,,,,,,,,,,,1,999,code,1,2,3,0,0,,0",
        ]
    }

    players = parse_follow_api_json(__import__("json").dumps(payload, ensure_ascii=False), "https://eiketsu-taisen.net")

    assert players == [
        {
            "follow_id": "425521",
            "name": "ルルルカ",
            "state": "食客",
            "daily_url": "https://eiketsu-taisen.net/members/history/daily?f=425521",
            "today": "1",
            "now": "1",
            "lastplaytime": "1778463427",
        }
    ]


def test_parse_detail_html_extracts_match_fields_and_decks():
    seed = {
        "detail_url": "https://eiketsu-taisen.net/members/history/detail?t=1773932045&f=586",
        "played_at": "2026-03-20 23:54",
        "mode": "全国対戦",
        "result": "loss",
        "follow_id": "586",
        "player_name": "最強の初心者",
        "opponent_name": "どんべえ",
        "castle_rates": ["82.00%", "0.00%"],
    }
    detail = parse_detail_html(DETAIL_HTML, seed["detail_url"], "https://eiketsu-taisen.net", seed)

    assert detail["replay_id"] == "b925ff0584d242fa895d47e03306a4b8"
    assert detail["m3u8_url"].endswith("/b925ff0584d242fa895d47e03306a4b8/master.m3u8")
    assert detail["version"] == "Ver.3.1.0H"
    assert detail["result"] == "win"
    assert detail["players"][0]["deck_ids"] == ["hash-a", "hash-b"]
    assert detail["players"][1]["deck_ids"] == ["hash-c", "hash-d", "hash-e"]
    assert detail["players"][0]["profile"]["段位"] == "旅人"
    assert detail["players"][1]["profile"]["段位"] == "騎士1"
    assert detail["players"][0]["battle_stats"]["kill_count"]["total"] == 5
    assert detail["players"][0]["battle_stats"]["death_count"]["total"] == 5
    assert detail["players"][1]["battle_stats"]["kill_count"]["total"] == 6
    assert detail["players"][0]["selected"]["weapon"]["name"] == "Weapon Test"
    assert detail["players"][0]["selected"]["school"]["name"] == "Style Test"
    assert detail["players"][0]["selected"]["souls"] == [{"name": "Soul A"}, {"name": "Soul B"}]
    assert detail["timeline_data"]["castle"]["enemy"] == [10000, 0]


def test_parse_replay_html_extracts_replay_id_and_decks():
    parsed = parse_replay_html(REPLAY_HTML, "https://example.test/local.html", "https://eiketsu-taisen.net")

    assert parsed["replay_id"] == "b925ff0584d242fa895d47e03306a4b8"
    assert parsed["player1_deck_ids"] == ["hash-a", "hash-b"]
    assert parsed["player2_deck_ids"] == ["hash-c"]
    assert parsed["player_names"] == ["Player A", "Opponent B"]
    assert parsed["follow_ids"] == ["586", ""]


def test_environment_mode_filter_skips_solo_modes_by_default():
    assert is_environment_mode("全国対戦")
    assert not is_environment_mode("戦祭り")
    assert not is_environment_mode("群雄伝")
    assert not is_environment_mode("鍛練場")
    assert is_environment_mode("戦祭り", include_solo=True)
