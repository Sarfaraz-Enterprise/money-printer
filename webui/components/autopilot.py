"""Scheduler controls; never starts workers or publishes from a browser rerun."""

from datetime import datetime

from filelock import Timeout
from pydantic import ValidationError
import pytz
import streamlit as st

from app.services import accounts, agent, scheduler, voice


def render_autopilot_panel(tr):
    with st.expander(tr("Auto-Pilot Scheduler"), expanded=False):
        st.caption(tr("Auto-Pilot Help"))
        try:
            settings = scheduler.load_settings()
            status = scheduler.scheduler_status()
            history = scheduler.recent_history()
        except (OSError, ValueError, Timeout):
            st.error(tr("Auto-Pilot Settings Read Failed"))
            return

        st.write(tr("Auto-Pilot Running") if status["running"] else tr("Auto-Pilot Stopped"))
        if not status["worker_alive"]:
            st.warning(tr("Auto-Pilot Worker Required"))
        if status.get("error_type"):
            st.error(tr("Auto-Pilot Worker Error"))
        for timestamp in status.get("next_runs", []):
            run = datetime.fromisoformat(timestamp)
            audience_run = run.astimezone(pytz.timezone(status["timezone"]))
            st.write(
                tr("Auto-Pilot Next Run") + ": "
                + audience_run.strftime("%Y-%m-%d %H:%M %Z")
                + " | " + tr("Auto-Pilot Server Time") + ": "
                + run.astimezone().strftime("%Y-%m-%d %H:%M %Z")
            )

        with st.form("autopilot_settings"):
            profiles = accounts.list_profiles()
            profile_names = {row["id"]: row["name"] for row in profiles}
            target_ids = st.multiselect(
                "Target Accounts / Channels", list(profile_names),
                default=[item for item in settings.target_account_ids if item in profile_names],
                format_func=lambda value: profile_names[value], key="autopilot_target_accounts",
            )
            st.caption("Selected accounts use isolated direct publishers. Leave empty for the legacy Upload-Post account.")
            enabled = st.toggle(tr("Enable Daily Auto-Pilot"), value=settings.enabled, key="autopilot_enabled")
            zones = list(scheduler.TIMEZONES)
            zone = st.selectbox(
                tr("Target Audience Timezone"), zones, index=zones.index(settings.timezone),
                format_func=lambda value: scheduler.TIMEZONES[value], key="autopilot_timezone",
            )
            first, second = st.columns(2)
            slots = [
                first.time_input(tr("Auto-Pilot Slot 1"), datetime.strptime(settings.slots[0], "%H:%M").time(), key="autopilot_slot_1"),
                second.time_input(tr("Auto-Pilot Slot 2"), datetime.strptime(settings.slots[1], "%H:%M").time(), key="autopilot_slot_2"),
            ]
            youtube = st.checkbox("YouTube Shorts", value="youtube" in settings.platforms, key="autopilot_youtube")
            instagram = st.checkbox("Instagram Reels", value="instagram" in settings.platforms, key="autopilot_instagram")
            st.selectbox(
                "Niche Style", [agent.AFTERFRAME_STYLE], disabled=True,
                key="autopilot_niche_style",
            )
            sub_theme = st.text_input(
                "Sub-theme Focus", value=settings.sub_theme_focus, max_chars=2000,
                placeholder="Time loops, Glitched tech, Doppelgängers",
                key="autopilot_sub_theme",
            )
            st.caption("Locked to original Afterframe cinematic stories: 75-95 words. Internet trends and legacy niche prompts are not used.")
            voices = [voice.ELEVENLABS_SERVER_DEFAULT_VOICE, *voice.get_elevenlabs_catalog_voices()]
            selected_voice = st.selectbox(
                tr("Auto-Pilot Voice"), voices, index=voices.index(settings.voice_name),
                format_func=lambda value: value.split(":", 2)[2], key="autopilot_voice",
            )
            sources = ["pexels", "pixabay", "coverr"]
            source = st.selectbox(tr("Auto-Pilot Stock Source"), sources, index=sources.index(settings.video_source), key="autopilot_source")
            made_for_kids = st.checkbox(tr("Auto-Pilot Made for Kids"), value=settings.youtube_made_for_kids, key="autopilot_kids")
            confirm = st.checkbox(tr("Auto-Pilot Publishing Consent"), value=False, key="autopilot_consent")
            if st.form_submit_button(tr("Save Auto-Pilot Settings"), type="primary"):
                if enabled and not confirm:
                    st.error(tr("Auto-Pilot Consent Required"))
                else:
                    try:
                        scheduler.save_settings(scheduler.ScheduleSettings(
                            enabled=enabled, timezone=zone,
                            slots=[slot.strftime("%H:%M") for slot in slots],
                            platforms=(["youtube"] if youtube else []) + (["instagram"] if instagram else []),
                            niche_style="afterframe", sub_theme_focus=sub_theme, voice_name=selected_voice,
                            target_account_ids=target_ids,
                            video_source=source, youtube_made_for_kids=made_for_kids,
                        ))
                    except (ValidationError, ValueError, OSError, Timeout) as exc:
                        st.error(str(exc))
                    else:
                        st.success(tr("Auto-Pilot Settings Saved"))
        with st.expander(tr("Recent Auto-Published Runs")):
            if history:
                st.dataframe(list(reversed(history[-30:])), use_container_width=True)
            else:
                st.caption(tr("No Auto-Pilot Runs"))
