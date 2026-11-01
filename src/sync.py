
from config import Config
from gcalendar import GoogleCalendarClient
from logger import logger
from models import Event
from timetree import TimeTree


def _private_properties(google_event: dict) -> dict:
    return google_event.get("extendedProperties", {}).get("private", {})


def _is_managed_google_event(google_event: dict, calendar_code: str) -> bool:
    """Return True only for events owned by this TimeTree sync."""
    props = _private_properties(google_event)
    timetree_id = props.get("timetree_id")

    if not timetree_id:
        return False

    sync_source = props.get("sync_source")
    source_calendar_code = props.get("timetree_calendar_code")

    if sync_source is None and source_calendar_code is None:
        return True

    return (
        sync_source == Event.SYNC_SOURCE
        and source_calendar_code == calendar_code
    )


def sync():
    client = TimeTree()
    client.login(
        Config.TIMETREE_EMAIL,
        Config.TIMETREE_PASSWORD,
    )

    calendar = client.get_calendar(
        Config.TIMETREE_CALENDAR_CODE,
    )
    logger.info("Selected TimeTree calendar")

    raw_events = client.get_events(calendar)

    if not isinstance(raw_events, list):
        raise RuntimeError(
            "TimeTree returned an invalid event list. Sync aborted."
        )

    events = [Event.from_timetree(raw) for raw in raw_events]

    google = GoogleCalendarClient(
        Config.GOOGLE_SERVICE_ACCOUNT_JSON,
    )
    logger.info("Connected to Google Calendar")

    google_events = google.list_events(
        Config.GOOGLE_CALENDAR_ID,
    )

    google_event_map: dict[str, list[dict]] = {}

    for google_event in google_events:
        if not _is_managed_google_event(
            google_event,
            Config.TIMETREE_CALENDAR_CODE,
        ):
            continue

        timetree_id = _private_properties(google_event)["timetree_id"]
        google_event_map.setdefault(timetree_id, []).append(google_event)

    created_count = 0
    updated_count = 0
    skipped_count = 0

    for event in events:
        matches = google_event_map.get(event.id, [])

        if not matches:
            google.create_event(
                Config.GOOGLE_CALENDAR_ID,
                event.to_google(Config.TIMETREE_CALENDAR_CODE),
            )
            created_count += 1
            continue

        google_event = matches[0]
        props = _private_properties(google_event)

        needs_marker_migration = (
            props.get("sync_source") != Event.SYNC_SOURCE
            or props.get("timetree_calendar_code")
            != Config.TIMETREE_CALENDAR_CODE
        )

        if needs_marker_migration or not event.equals_google(
            google_event,
            Config.TIMETREE_CALENDAR_CODE,
        ):
            google.update_event(
                Config.GOOGLE_CALENDAR_ID,
                google_event["id"],
                event.to_google(Config.TIMETREE_CALENDAR_CODE),
            )
            updated_count += 1
        else:
            skipped_count += 1

    logger.info(
        "Sync complete: created=%d updated=%d deleted=0 skipped=%d",
        created_count,
        updated_count,
        skipped_count,
    )
