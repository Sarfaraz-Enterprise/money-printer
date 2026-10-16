"""English-only Srz AutoFlow workspace over the existing rendering pipeline."""

import os
from datetime import datetime
from pathlib import Path

import pytz
import streamlit as st
from filelock import Timeout
from loguru import logger

from app.config import config
from app.models import const
from app.services import accounts, autoflow_settings, autoflow_studio, master_agent, profile_schedules, scheduler, state, voice
from app.services.account_dispatcher import dispatch_video, recent_dispatches
from app.utils.logging_utils import configure_terminal_logger
from app.utils import utils

TITLE = "Srz AutoFlow"
SUBTITLE = "Autonomous Multi-Channel Content & Distribution Engine"
IST = pytz.timezone("Asia/Kolkata")
ENGLISH_EDGE_VOICES = ["en-US-AndrewNeural-Male", "en-US-AvaNeural-Female",
                       "en-US-GuyNeural-Male", "en-US-JennyNeural-Female"]


def _notice(message):
    st.toast(message)


def _error(message):
    st.toast("🔴 Error: " + message)
    st.caption("🔴 Error · " + message)


def _results(rows):
    if not rows:
        st.caption("No publishing activity yet.")
        return
    st.dataframe([
        {"Profile": row.get("name") or row.get("profile_id", ""),
         "Platform": row.get("platform", ""),
         "Status": "🟢 Active" if row.get("success") else "🟡 Pending" if row.get("status") in ("pending", "uncertain") else "🔴 Inactive",
         "URL": row.get("url") or "", "Details": row.get("error_code") or row.get("error") or ""}
        for row in rows
    ], hide_index=True, width="stretch")


def _profile_select(profiles, label, key):
    names = {row["id"]: row["name"] for row in profiles}
    return st.selectbox(label, list(names), format_func=lambda item: names[item], key=key)


@st.fragment(run_every="2s")
def _task_result(task_id, authorized, targets):
    snapshot = state.state.get_task(task_id) or {}
    status = snapshot.get("state")
    progress = int(snapshot.get("progress", 0) or 0)
    phases = [
        "[1/4] Writing Script", "[2/4] Generating Voiceover",
        "[3/4] Curating B-Roll", "[4/4] Rendering & Dispatching",
    ]
    index = 0 if progress < 20 else 1 if progress < 40 else 2 if progress < 50 else 3
    if status == const.TASK_STATE_FAILED:
        st.error(snapshot.get("error") or "Video generation failed. Check system diagnostics.")
        return
    st.progress(min(max(progress, 0), 100), text=phases[index])
    if status != const.TASK_STATE_COMPLETE:
        st.caption("🟡 Pending — generation is running in the background.")
        return
    st.success("Video ready")
    for index, file in enumerate(snapshot.get("videos") or []):
        if not Path(file).is_file():
            st.error("A generated file is no longer available.")
            continue
        st.video(file)
        with open(file, "rb") as handle:
            st.download_button("Download MP4", handle.read(), file_name=Path(file).name,
                               mime="video/mp4", key=f"af-download-{task_id}-{index}")
        if snapshot.get("cross_post_state"):
            st.caption("Publishing: " + str(snapshot["cross_post_state"]))
            _results(snapshot.get("cross_post_results") or [])
            continue
        dispatched = st.session_state.get(f"af-posted-{task_id}-{index}")
        if dispatched:
            _results(dispatched)
        elif st.button("Post to selected channels", key=f"af-post-{task_id}-{index}",
                       disabled=not authorized or not targets):
            # Mark before the public operation; uncertain results must not be retried blindly.
            st.session_state[f"af-posted-{task_id}-{index}"] = [
                {"status": "uncertain", "error": "Publishing started. Inspect history before retrying."}
            ]
            rows = dispatch_video(file, snapshot.get("video_subject") or Path(file).stem,
                                  snapshot.get("video_description") or snapshot.get("script") or "", targets)
            st.session_state[f"af-posted-{task_id}-{index}"] = rows
            _results(rows)


def _clear_prepared_story():
    st.session_state.pop("af-written-subject", None)
    st.session_state.pop("af-written-keywords", None)


def _studio(profiles, authorized):
    st.subheader("Manual Studio")
    if not profiles:
        with st.container(key="af-onboarding"):
            st.markdown('<p class="af-eyebrow">YOUR FIRST FLIGHT</p>', unsafe_allow_html=True)
            st.subheader("A great story starts with a channel.")
            st.write("Give your AI writer an audience, a creative direction and a voice. Then create your first private video preview.")
            steps = st.columns(3)
            for column, title, text in (
                (steps[0], "01 / Build your profile", "Open Channel Profiles and add your channel identity."),
                (steps[1], "02 / Check your services", "Use System Health to check your configured APIs."),
                (steps[2], "03 / Generate a concept", "Return here for a private test flight. No automatic posting."),
            ):
                column.markdown("**" + title + "**")
                column.caption(text)
            if st.button("Create your first profile", type="primary", key="af-create-first"):
                _profile_modal()
        return
    st.caption("Your creative workspace. Generate a private test flight, or refine a script before rendering.")
    selected = _profile_select(profiles, "Select Profile", "af-studio-profile")
    profile = next(row for row in profiles if row["id"] == selected)
    if st.session_state.get("af-script-profile") != selected:
        st.session_state["af-script"] = ""
        st.session_state["af-topic"] = ""
        st.session_state["af-description"] = ""
        st.session_state["af-keywords"] = ""
        st.session_state["af-script-profile"] = selected
        st.session_state.pop("af-written-subject", None)
        st.session_state.pop("af-written-keywords", None)
    st.caption(profile.get("category", profile.get("niche", "General")) + " · " + profile.get("target_audience", "United States (US Tier-1)"))
    if st.button("Generate Concept", type="primary", key="af-concept"):
        try:
            task_id = autoflow_studio.submit(
                selected, "", "", profile.get("voice_name") or voice.ELEVENLABS_SERVER_DEFAULT_VOICE,
                st.session_state.get("af-stock", "pexels"), [], False,
            )
        except (ValueError, OSError, Timeout):
            _error("Concept generation could not start. Check system health or wait for the active render.")
        else:
            st.session_state["af-current-task"] = task_id
            st.session_state["af-task-targets"] = [selected]
            _notice("Test flight started: concept, QA, voice and video. No automatic dispatch.")
    st.caption("One click creates a private preview with this profile's audience and creative directions. Dispatch is a separate action.")
    topic = st.text_input("Topic / Video Subject", key="af-topic", on_change=_clear_prepared_story,
                          placeholder="Enter an idea, or let your profile's AI writer choose")
    if st.button("Write Script Only", type="secondary"):
        from app.services.profile_content import generate_profile_package
        try:
            with st.spinner("Writing a profile-bound story…"):
                package = generate_profile_package(profile, topic)
            st.session_state["af-script"] = package["script"]
            st.session_state["af-written-subject"] = package["subject"]
            st.session_state["af-written-keywords"] = package["keywords"]
            st.session_state["af-keywords"] = package["keywords"]
            st.session_state["af-description"] = package.get("description", "")
            _notice("Script generated for " + profile["name"])
        except (ValueError, OSError):
            _error("AI writing failed. Check Settings & System Health.")
    script = st.text_area("Narration Script", key="af-script", height=200,
                          placeholder="Optional manual narration, or use Generate Concept for a complete test flight")
    description = st.text_area("Video Description / Caption", key="af-description", max_chars=5000,
                               help="Publishing caption only; it does not replace your spoken narration.")
    keywords = st.text_input("Visual Keywords (comma-separated)", key="af-keywords",
                             help="Your stock footage search terms. Blank uses the profile or AI keywords.")
    st.caption("AI subject: " + st.session_state["af-written-subject"] if st.session_state.get("af-written-subject") else "Your script remains fully editable.")
    first, second = st.columns(2)
    provider = first.selectbox("Voice Engine", ["ElevenLabs", "Edge TTS (English)"], key="af-voice-engine")
    voices = [voice.ELEVENLABS_SERVER_DEFAULT_VOICE, *voice.get_elevenlabs_catalog_voices()] if provider == "ElevenLabs" else ENGLISH_EDGE_VOICES
    if provider == "ElevenLabs" and profile.get("voice_name") and profile["voice_name"] not in voices:
        voices.append(profile["voice_name"])
    selected_voice = second.selectbox("Voice", voices, index=voices.index(profile["voice_name"]) if profile.get("voice_name") in voices else 0,
                                     format_func=lambda item: item.split(":", 2)[-1] if item.startswith("elevenlabs:") else item, key=f"af-voice-{provider}-{selected}")
    source = st.selectbox("Stock Footage", ["pexels", "pixabay"], key="af-stock")
    names = {row["id"]: row["name"] for row in profiles}
    targets = st.multiselect("Target Accounts / Channels", list(names), default=[selected],
                            format_func=lambda item: names[item], key=f"af-targets-{selected}")
    if not authorized:
        st.caption("Dispatch is off. Generation saves a private preview only.")
    if st.button("Generate Video", key="af-generate"):
        try:
            title = st.session_state.get("af-written-subject") or topic or (profile["name"] if script.strip() else "")
            task_id = autoflow_studio.submit(selected, title, script, selected_voice, source,
                                            targets if authorized else [], authorized,
                                            keywords, description)
        except (ValueError, OSError, Timeout) as exc:
            _error("Render could not start. Check profile settings or wait for the active render.")
        else:
            st.session_state["af-current-task"] = task_id
            st.session_state["af-task-targets"] = targets
            _notice("Generation started")
    if task_id := st.session_state.get("af-current-task"):
        _task_result(task_id, authorized, st.session_state.get("af-task-targets") or [])


@st.fragment(run_every="30s")
def _schedule_status():
    now = datetime.now(IST)
    status = scheduler.scheduler_status()
    label = ("🔴 Offline" if not status.get("worker_alive")
             else "🟢 Active" if status.get("running") and profile_schedules.master_enabled()
             else "🟡 Standby")
    st.caption(label +
            " · Current Time IST: " + now.strftime("%d %b %Y, %I:%M %p"))
    if not status.get("worker_alive"):
        st.warning("Scheduler worker is offline. Start the backend or scheduler worker.")


def _schedules():
    st.subheader("Auto-Pilot Scheduler")
    _schedule_status()
    st.toggle("⚡ Master Autonomous Engine (Active / Standby)",
              value=profile_schedules.master_enabled(), key="af-master-agent",
              on_change=_master_changed)
    st.caption("Active authorizes scheduled publishing and automatic comment replies for enabled profiles. Edit independent posting times in Channel Profiles. Pausing prevents new actions, but cannot recall accepted uploads or replies.")
    _master_control()
    _agent_overview()
    with st.expander("Scheduler Logs"):
        rows = scheduler.recent_history()[-30:]
        if rows:
            st.dataframe([{key: row.get(key, "") for key in ("started_at", "profile_id", "subject", "status", "stage")} for row in rows],
                         hide_index=True, width="stretch")
        else:
            st.caption("No scheduler runs yet.")


def _master_changed():
    try:
        profile_schedules.set_master_enabled(st.session_state["af-master-agent"])
    except (OSError, ValueError, Timeout):
        _error("Master state could not be saved. Check private storage permissions.")


@st.fragment(run_every="15s")
def _master_control():
    report = master_agent.status()
    with st.container(border=True):
        st.subheader("Master Agent · Operations")
        st.caption("One supervisor for scheduled content, API health, daily limits and profile-isolated comment replies.")
        columns = st.columns(3)
        columns[0].metric("Daily video limit", "1 / profile")
        columns[1].metric("Daily reply limit", str(report["policy"]["replies_per_profile_per_day"]) + " / profile")
        columns[2].metric("Supervisor", "Offline" if not report["worker_alive"] else "Active" if report["active"] else "Standby")
        store = report["job_counts"]
        queue = [store.get("jobs_queued", 0), store.get("jobs_running", 0)]
        st.caption(f"Durable job ledger · {queue[0]} queued · {queue[1]} running · "
                   f"{store.get('jobs_failed', 0)} failed · "
                   f"{store.get('posts_uncertain', 0)} uncertain posts")
        st.caption("Manual jobs still require approval. Automatic retries cover transient preflight only; paid generation, uploads and replies are never blindly repeated.")
        st.caption("Comment cost guard: at most 20 recent comments analysed per profile per rolling day; at most one reply per 15-minute poll.")
        st.caption("Comment replies require platform permissions. YouTube accounts connected before this upgrade need OAuth reconnection for comment access.")
        st.caption("Last supervisor heartbeat: " + report.get("heartbeat", "Waiting for the worker") +
                   " · Last remote health check: " + report.get("health_checked_at", "Not checked; standby uses local configuration only"))
        for alert in report.get("alerts", []):
            st.caption("Action needed · " + alert)
        failures = [
            {"Profile": row.get("profile_id", ""), "Stage": row.get("stage", ""),
             "Status": row.get("status", ""), "Error type": row.get("error_type", ""),
             "Retry after": row.get("retry_at", "")}
            for row in scheduler.recent_history()[-30:]
            if row.get("status") in {"failed", "partial_failure", "interrupted", "budget_blocked", "queued"}
        ]
        if failures:
            st.dataframe(failures, hide_index=True, width="stretch")
        if report.get("recent_jobs"):
            st.caption("Recent pipeline jobs · stages and error classes (no private paths or credentials)")
            st.dataframe([
                {key: row.get(key, "") for key in (
                    "job_id", "parent_job_id", "profile_id", "scheduled_date", "slot", "status",
                    "stage", "attempt", "error_class", "updated_at",
                )}
                for row in report["recent_jobs"]
            ], hide_index=True, width="stretch")
        if report.get("recent_stages"):
            st.caption("Stage checkpoints · attempts and outcomes")
            st.dataframe(report["recent_stages"], hide_index=True, width="stretch")
        if report.get("recent_artifacts"):
            st.caption("Recent render artifacts · SHA-256 fingerprints (paths remain private)")
            st.dataframe(report["recent_artifacts"], hide_index=True, width="stretch")
        if report.get("recent_posts"):
            st.caption("Per-platform publish outbox · uncertain/in-flight rows require review before retry")
            st.dataframe(report["recent_posts"], hide_index=True, width="stretch")
        if report.get("comments"):
            st.dataframe(report["comments"], hide_index=True, width="stretch")
        if report.get("reply_activity"):
            st.caption("Recent reply activity · uncertain or analysis-attempted actions are never automatically replayed.")
            st.dataframe(report["reply_activity"], hide_index=True, width="stretch")


@st.fragment(run_every="10s")
def _agent_overview():
    profiles = accounts.list_profiles()
    now = datetime.now(IST)
    due = profile_schedules.next_trigger(profiles, now)
    if due is not None:
        remaining = max(0, int((due - now).total_seconds()))
        st.metric("Next Trigger (IST)", due.strftime("%d %b %H:%M"),
                  f"{remaining // 3600:02d}:{remaining % 3600 // 60:02d}:{remaining % 60:02d} remaining")
    else:
        st.caption("🟡 Standby · No active trigger. Enable a profile schedule and activate the engine when ready.")
    rows = profile_schedules.timeline(profiles, now)
    st.subheader("Today's Timeline · IST")
    if rows:
        st.vega_lite_chart(rows, {
            "mark": {"type": "tick", "thickness": 5, "size": 28},
            "encoding": {
                "x": {"field": "Time (IST)", "type": "ordinal", "title": "Posting Time · IST", "sort": "ascending"},
                "y": {"field": "Profile", "type": "nominal"},
                "color": {"field": "Status", "type": "nominal"},
                "tooltip": [{"field": key, "type": "nominal"} for key in ("Profile", "Time (IST)", "Audience Time", "Status", "Platforms")],
            },
            "height": max(140, len(profiles) * 45),
        }, width="stretch")
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption("Add a profile to build your daily timeline.")
    if profiles:
        st.dataframe(profile_schedules.overview(profiles, now), hide_index=True, width="stretch")


@st.dialog("Add / Edit Profile", width="large")
def _profile_modal(profile=None):
    from webui.components.accounts_manager import _profile_form
    _profile_form(profile)


def _profiles(profiles):
    st.subheader("Channel Profiles")
    st.caption("One identity per channel. Keep audiences, creative directions, credentials and schedules isolated.")
    if st.button("Add Profile", type="primary"):
        _profile_modal()
    if not profiles:
        st.info("No channels yet. Add a profile to define its audience, narrator and independent posting times. Connecting a social account can come later.")
    for profile in profiles:
        identifier = profile["id"]
        with st.container(border=True):
            st.subheader("◈ " + profile["name"])
            st.caption(profile.get("niche", "General"))
            st.write("Audience: " + profile.get("target_audience", "United States (US Tier-1)"))
            st.caption("Audience zone: " + profile.get("timezone", "America/New_York") +
                       " · Tags: " + ", ".join(profile.get("audience_tags", [])))
            schedule = profile.get("schedule", {})
            st.caption(("🟡 Scheduled" if schedule.get("enabled") else "Standby") + " · " +
                       ", ".join(schedule.get("post_times_ist", schedule.get("post_times", schedule.get("slots", [])))) + " IST")
            st.write("YouTube: " + ("🟢 Connected via local OAuth" if profile["youtube"].get("connected") else "🔴 Not Connected"))
            st.write("Facebook: " + ("🟢 Page ID configured" if profile["facebook"].get("page_id") else "🔴 Not configured"))
            st.write("Instagram: " + ("🟢 Business Account ID configured" if profile["instagram"].get("business_account_id") else "🔴 Not configured"))
            st.caption("Configured identifiers do not guarantee live publishing permissions.")
            columns = st.columns(4)
            if columns[0].button("Edit Profile", key=f"af-edit-{identifier}"):
                _profile_modal(profile)
            if columns[1].button("Connect YouTube", key=f"af-yt-{identifier}"):
                try:
                    st.session_state[f"af-oauth-{identifier}"] = accounts.start_youtube_oauth(identifier)
                except (ValueError, OSError):
                    _error("Set Google Client ID and Secret in the root .env; use a Desktop OAuth client.")
            if columns[2].button("Check Connections", key=f"af-check-{identifier}"):
                check = accounts.validate_profile(identifier)
                st.dataframe([{"Platform": key, "Result": str(value.get("code") or value.get("status") or value.get("ok"))}
                              for key, value in check.items() if isinstance(value, dict)], hide_index=True)
            if columns[3].button("Delete", key=f"af-delete-{identifier}"):
                st.session_state["af-delete-profile"] = identifier
            if st.session_state.get("af-delete-profile") == identifier:
                st.warning("Delete this profile and its local OAuth token? Existing videos will remain.")
                if st.button("Confirm deletion", key=f"af-confirm-delete-{identifier}"):
                    accounts.delete_profile(identifier)
                    st.session_state.pop("af-delete-profile", None)
                    st.rerun()
            if url := st.session_state.get(f"af-oauth-{identifier}"):
                st.link_button("Open local YouTube consent", url)
                if st.button("Refresh OAuth Status", key=f"af-refresh-{identifier}"):
                    result = accounts.oauth_status(identifier)
                    st.info("OAuth: " + str(result.get("status", "Pending")))
    with st.expander("Distribution History"):
        st.button("Refresh history", key="af-history-refresh")
        _results(recent_dispatches())


def _vault():
    st.subheader("Settings & System Health")
    st.caption("Read-only root .env inspection. No key-entry fields, no secret values sent to the browser.")
    board = autoflow_settings.health_board()
    signature = autoflow_settings.credential_revision()
    if st.session_state.get("af-health-config") != signature:
        st.session_state.pop("af-health-results", None)
        st.session_state["af-health-config"] = signature
    if st.button("Run Health Check", key="af-diagnostics"):
        with st.spinner("Checking read-only API connectivity…"):
            st.session_state["af-health-results"] = autoflow_settings.diagnostics()
        _notice("Health check complete. See service status and latency below.")
    st.dataframe(st.session_state.get("af-health-results", board), hide_index=True, width="stretch")
    st.caption("Before testing, green means credentials exist in .env. HTTP checks verify read access, not billing, TTS generation or publish scopes. Google needs a connected OAuth profile; client strings alone cannot prove authorization.")
    st.caption("Current LLM: " + str(config.app.get("llm_provider", "anthropic")) + " · Credential changes are made only in the root .env.")
    from app.services.llm import gateway_configuration_preview
    proposed = gateway_configuration_preview()
    with st.container(border=True):
        st.markdown("**Proposed LLM · Local-only configuration**")
        st.write(proposed["provider"])
        st.caption("🟡 Inactive / Not validated · Existing generation and scheduler routing are unchanged.")
        st.caption("OPENAI_API_KEY: " + ("Present in root .env; not sent or validated" if proposed["credential_present"] else "Missing in root .env"))
        st.caption("Settings: OPENAI_BASE_URL and OPENAI_MODEL_NAME in the root .env. No APInex requests are made, including by Run Health Check.")
    st.caption("FFmpeg: " + ("Available" if utils.check_ffmpeg_ready() else "Not available"))


@st.fragment(run_every="30s")
def _workspace_pulse():
    profiles = accounts.list_profiles()
    worker = scheduler.scheduler_status()
    active = profile_schedules.master_enabled()
    scheduled = sum(
        profile.get("enabled", True) and profile.get("schedule", {}).get("enabled", False)
        for profile in profiles
    )
    connections = sum(
        bool(profile.get(platform, {}).get("connected"))
        for profile in profiles for platform in ("youtube", "facebook", "instagram")
    )
    columns = st.columns(4)
    columns[0].metric("Channel profiles", str(len(profiles)))
    columns[1].metric("Enabled schedules", str(scheduled))
    columns[2].metric("Linked accounts", str(connections),
                      help="Local credential presence only. This does not verify live platform permissions.")
    engine = "Offline" if not worker.get("worker_alive") else "Active" if active else "Standby"
    columns[3].metric("Autonomous engine", engine)
    st.caption("Snapshot updated " + datetime.now(IST).strftime("%H:%M:%S IST") +
               " · Refreshes every 30s · Engine status is based on the worker heartbeat and master switch.")


def main(configure_page=True):
    if configure_page:
        st.set_page_config(page_title=TITLE, page_icon="⚡", layout="wide")
    import sys
    configure_terminal_logger(sys.stderr, config.log_level, colorize=False)
    css = Path(__file__).with_name("dashboard.css").read_text(encoding="utf-8")
    st.markdown("<style>" + css + "</style>", unsafe_allow_html=True)
    st.markdown('<p class="af-eyebrow">PRIVATE CONTENT OPERATIONS</p>', unsafe_allow_html=True)
    st.title("⚡ " + TITLE)
    st.caption(SUBTITLE)
    with st.sidebar:
        st.header("⚡ " + TITLE)
        st.caption("Private multi-channel workspace")
        st.caption("English · IST scheduling · Isolated profiles")
        st.markdown('<p class="af-sidebar-label">WORKSPACE GUIDE</p>', unsafe_allow_html=True)
        st.markdown("**01** Create in Manual Studio\n\n**02** Automate your schedule\n\n**03** Manage channel identities\n\n**04** Monitor system health")
        st.markdown('<p class="af-sidebar-label">PRIVATE BY DEFAULT</p>', unsafe_allow_html=True)
        st.caption("Previews stay local. Public posting requires manual authorization or an active scheduled engine.")
    try:
        profiles = accounts.list_profiles()
        _workspace_pulse()
        tabs = st.tabs(["🎬 Manual Studio", "⏰ Auto-Pilot Scheduler", "👥 Channel Profiles", "⚙️ Settings & System Health"])
        with tabs[0]:
            with st.container(key="af-dispatch-consent"):
                authorized = st.toggle("Authorize automated multi-platform dispatch", value=False, key="af-authorize")
                st.caption("Manual Studio only: off renders private previews. Scheduled publishing is controlled independently by the master agent.")
            _studio(profiles, authorized)
        with tabs[1]:
            _schedules()
        with tabs[2]:
            _profiles(profiles)
        with tabs[3]:
            _vault()
    except (ValueError, OSError, Timeout) as exc:
        logger.error(f"Workspace operation failed: {type(exc).__name__}")
        _error("This operation could not complete. Check profile configuration and private storage permissions.")
