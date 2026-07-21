"""Central CSS selectors for active AFL scraper implementations."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FixtureSelectors:
    """Selectors for the AFL fixtures landing page and round navigation."""

    METADATA_ROOT_CLASS = "js-react-fixtures"
    ROUND_LIST_ITEMS = "ul.competition-nav__round-list > li"
    ROUND_LABEL_BUTTON = "button"


@dataclass(frozen=True)
class MatchCardSelectors:
    """Selectors for AFL fixture match cards shared by match scrapers/monitors."""

    SEASON_LABEL = "div.competition-nav__season-select .select__current-text"
    DATE_HEADER_OR_MATCH_CARD = "h2.fixtures__date-header, div.fixtures__item"
    DATE_HEADER_CLASS = "fixtures__date-header"
    MATCH_CARD = "div.fixtures__item"
    MATCH_CARD_CLASS = "fixtures__item"
    HOME_TEAM_NAME = ".fixtures__match-team--home span"
    AWAY_TEAM_NAME = ".fixtures__match-team--away span"
    VENUE = ".fixtures__match-venue"
    DETAILS_LINK = "a.fixtures__absolute-link"
    MATCH_TIME = ".fixtures__match-time"
    STATUS_LABEL = ".fixtures__status-label"
    SCORE_TOTAL = ".fixtures__match-score-total"
    LIVE_CLOCK = "span.js-match-clock"


@dataclass(frozen=True)
class TeamLineupSelectors:
    """Selectors for the AFL team lineups page."""

    MATCH_ITEM = "div.team-lineups__item"
    MATCH_ITEM_READY = ".team-lineups__item"
    MATCH_HEADER_LINK = "a.team-lineups-header"
    MATCH_HEADER_NAME = ".team-lineups-header__name"
    HOME_INS_AND_OUTS_GRID_ITEMS = (
        ".team-lineups-ins-and-outs__grid--home "
        ".team-lineups-ins-and-outs__grid-item"
    )
    AWAY_INS_AND_OUTS_GRID_ITEMS = (
        ".team-lineups-ins-and-outs__grid--away "
        ".team-lineups-ins-and-outs__grid-item"
    )
    INS_AND_OUTS_PLAYER_NAME = ".team-lineups-ins-and-outs__player-name"
    PLAYER_ENTRY = "a.team-lineups__player-entry"
    PLAYER_ENTRY_FIRST_NAME = ".team-lineups__player-entry--name-first"
    PLAYER_ENTRY_LAST_NAME = ".team-lineups__player-entry--name-last"
    HOME_PLAYER_ENTRY_CLASS = "team-lineups__player-entry--home-team"
    ROUND_LIST_READY = ".competition-nav__round-list"
    ROUND_BUTTON_BY_ID_TEMPLATE = 'li[data-round-id="{round_number}"] button'
    EXPAND_LINEUPS_TOGGLE_LABEL = 'label[for="expand-lineups-toggle"]'


@dataclass(frozen=True)
class PlayerStatsSelectors:
    """Selectors for AFL match-centre player stats pages."""

    MATCH_STATUS_LABEL = "span.mc-header__status-label"
    STATS_TABLE = "table.stats-table__table"
    HEADER_CELLS = "thead tr.stats-table__header-row th"
    BODY_ROWS = "tbody.stats-table__body-row, tr.stats-table__body-row"
    PLAYER_PROFILE_LINK = "a.mc-player-stats-table__player"
    PLAYER_HEADSHOT = "img.mc-player-stats-table__headshot"
    JUMPER_NUMBER = "span.mc-player-stats-table__jumper-number"


@dataclass(frozen=True)
class InjurySelectors:
    """Selectors for the AFL injury list page."""

    ARTICLE_BODY = "div.article__body"
    TEAM_BLOCKS = "div.articleWidget.full-width"
    PROMO_IMAGE_CLASS = "promo-image__image"


@dataclass(frozen=True)
class StatsLeadersSelectors:
    """Selectors for AFL stats leaders tables."""

    SCROLL_CONTAINER = "div.js-scrollable-container"
    PLAYER_IMAGES = "img.picture__img"
    FINAL_BODY_ROW = "tr.stats-table__body-row:last-child"
    LOAD_MORE_BUTTON = "button.stats-table-load-more-button"
    BODY_ROWS = "tr.stats-table__body-row"
    PLAYER_NAME_LINK = "a.stats-leaders-table-player__name"
    PLAYER_HEADSHOT = ".stats-leaders-table-player__headshot"
    STAT_BUTTONS = "td.stats-table__cell button"


@dataclass(frozen=True)
class ClubSquadSelectors:
    """Selectors for AFL club squad pages."""

    SQUAD_CARD = ".squad-list__item"
    PLAYER_LINK = "a.player-item"
    FIRST_NAME = "h1.player-item__name"
    LAST_NAME = ".player-item__last-name"
    POSITION = ".player-item__position"
    JUMPER_NUMBER = ".player-item__jumper-number"
    PRIMARY_IMAGE = "img.picture__img"
    FALLBACK_IMAGE = "picture img"


FIXTURE_SELECTORS = FixtureSelectors()
MATCH_CARD_SELECTORS = MatchCardSelectors()
TEAM_LINEUP_SELECTORS = TeamLineupSelectors()
PLAYER_STATS_SELECTORS = PlayerStatsSelectors()
INJURY_SELECTORS = InjurySelectors()
STATS_LEADERS_SELECTORS = StatsLeadersSelectors()
CLUB_SQUAD_SELECTORS = ClubSquadSelectors()
