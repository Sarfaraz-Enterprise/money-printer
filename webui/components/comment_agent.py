"""Social comment agent controls; the browser never polls, replies or renders."""

from datetime import datetime, timedelta, timezone

from filelock import Timeout
from pydantic import ValidationError
import streamlit as st

from app.services import comment_agent, voice


PLATFORM_LABELS = {"youtube": "YouTube Shorts", "instagram": "Instagram Reels", "facebook": "Facebook Page"}


def _recent(records, field, hours=24):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [
        record for record in records
        if (moment := comment_agent._parse_time(record.get(field))) and moment > cutoff
    ]


def render_comment_agent_panel(tr):
    with st.expander(tr("Social Comment Agent"), expanded=False):
        st.caption(tr("Comment Agent Help"))
        try:
            settings = comment_agent.load_settings()
            state = comment_agent.load_state()
            status = comment_agent.agent_status()
        except (OSError, ValueError, Timeout):
            st.error(tr("Comment Agent Read Failed"))
            return
        credentials = comment_agent.credential_status()
        comments = list(state["comments"].values())
        ideas = state["ideas"]

        st.write(tr("Comment Agent Running") if status["running"] else tr("Comment Agent Stopped"))
        if not status["worker_alive"]:
            st.warning(tr("Comment Agent Worker Required"))
        if status.get("error_type"):
            st.error(tr("Comment Agent Worker Error"))
        if status.get("next_poll"):
            st.write(tr("Comment Agent Next Poll") + ": " + datetime.fromisoformat(
                status["next_poll"]).astimezone().strftime("%Y-%m-%d %H:%M %Z"))

        columns = st.columns(5)
        columns[0].metric(tr("Comment Agent Channels"), sum(
            1 for platform in settings.platforms if credentials[platform]))
        columns[1].metric(tr("Comment Agent Analysed 24h"), len(_recent(comments, "seen_at")))
        columns[2].metric(tr("Comment Agent Replies 24h"), sum(
            1 for record in _recent(comments, "replied_at") if record.get("reply_status") == "replied"))
        columns[3].metric(tr("Comment Agent Ideas Queued"), sum(
            1 for idea in ideas if idea.get("status") == "queued"))
        columns[4].metric(tr("Comment Agent Videos Published"), sum(
            1 for idea in ideas if idea.get("status") == "complete"))
        st.dataframe([
            {
                "platform": PLATFORM_LABELS[platform],
                "credentials": "✅" if credentials[platform] else "❌ " + ", ".join(comment_agent.REQUIRED_ENV[platform]),
                "monitored": platform in settings.platforms,
                "last_error": ((status.get("last_cycle") or {}).get("platforms", {}).get(platform) or {}).get("error", ""),
            }
            for platform in comment_agent.PLATFORMS
        ], use_container_width=True, hide_index=True)

        with st.form("comment_agent_settings"):
            enabled = st.toggle("Enable Autonomous Comment & Content Agent", value=settings.enabled, key="comment_agent_enabled")
            st.markdown("**" + tr("Comment Agent Monitored Platforms") + "**")
            platform_columns = st.columns(3)
            monitored = [
                platform for column, platform in zip(platform_columns, comment_agent.PLATFORMS)
                if column.checkbox(PLATFORM_LABELS[platform], value=platform in settings.platforms,
                                   key=f"comment_agent_monitor_{platform}")
            ]
            first, second, third = st.columns(3)
            poll_minutes = first.number_input(tr("Comment Agent Poll Minutes"), 5, 240, settings.poll_minutes, key="comment_agent_poll_minutes")
            media_limit = second.number_input(tr("Comment Agent Media Limit"), 1, 25, settings.media_limit, key="comment_agent_media_limit")
            max_age = third.number_input(tr("Comment Agent Max Age"), 1, 168, settings.max_comment_age_hours, key="comment_agent_max_age")

            auto_reply = st.toggle(tr("Comment Agent Auto Reply"), value=settings.auto_reply, key="comment_agent_auto_reply")
            first, second, third = st.columns(3)
            per_hour = first.number_input(tr("Comment Agent Replies Per Hour"), 1, 60, settings.max_replies_per_hour, key="comment_agent_per_hour")
            per_day = second.number_input(tr("Comment Agent Replies Per Day"), 1, 300, settings.max_replies_per_day, key="comment_agent_per_day")
            interval = third.number_input(tr("Comment Agent Reply Interval"), 5, 300, settings.reply_interval_seconds, key="comment_agent_interval")
            brand_voice = st.text_input(tr("Comment Agent Brand Voice"), value=settings.brand_voice, max_chars=500, key="comment_agent_brand_voice")

            auto_video = st.toggle(tr("Comment Agent Auto Video"), value=settings.auto_video, key="comment_agent_auto_video")
            first, second = st.columns(2)
            min_score = first.slider(tr("Comment Agent Min Idea Score"), 1, 10, settings.min_idea_score, key="comment_agent_min_score")
            max_videos = second.number_input(tr("Comment Agent Videos Per Day"), 0, 4, settings.max_videos_per_day, key="comment_agent_max_videos")
            publish_columns = st.columns(2)
            publish = [
                platform for column, platform in zip(publish_columns, ("youtube", "instagram"))
                if column.checkbox(tr("Comment Agent Publish To") + " " + PLATFORM_LABELS[platform],
                                   value=platform in settings.publish_platforms,
                                   key=f"comment_agent_publish_{platform}")
            ]
            voices = [voice.ELEVENLABS_SERVER_DEFAULT_VOICE, *voice.get_elevenlabs_catalog_voices()]
            selected_voice = st.selectbox(
                tr("Auto-Pilot Voice"), voices, index=voices.index(settings.voice_name),
                format_func=lambda value: value.split(":", 2)[2], key="comment_agent_voice",
            )
            sources = ["pexels", "pixabay", "coverr"]
            source = st.selectbox(tr("Auto-Pilot Stock Source"), sources,
                                  index=sources.index(settings.video_source), key="comment_agent_source")
            made_for_kids = st.checkbox(tr("Auto-Pilot Made for Kids"), value=settings.youtube_made_for_kids,
                                        key="comment_agent_kids")
            consent = st.checkbox(tr("Comment Agent Consent"), value=False, key="comment_agent_consent")
            if st.form_submit_button(tr("Save Comment Agent Settings"), type="primary"):
                if enabled and not consent:
                    st.error(tr("Comment Agent Consent Required"))
                else:
                    try:
                        comment_agent.save_settings(comment_agent.CommentAgentSettings(
                            enabled=enabled, platforms=monitored, poll_minutes=int(poll_minutes),
                            media_limit=int(media_limit), max_comment_age_hours=int(max_age),
                            auto_reply=auto_reply, max_replies_per_hour=int(per_hour),
                            max_replies_per_day=int(per_day), reply_interval_seconds=int(interval),
                            brand_voice=brand_voice, auto_video=auto_video, min_idea_score=int(min_score),
                            max_videos_per_day=int(max_videos), publish_platforms=publish,
                            voice_name=selected_voice, video_source=source,
                            youtube_made_for_kids=made_for_kids,
                        ))
                    except (ValidationError, ValueError, OSError, Timeout) as exc:
                        st.error(str(exc))
                    else:
                        st.success(tr("Comment Agent Settings Saved"))

        with st.expander(tr("Comment Agent Recent Comments")):
            rows = sorted(comments, key=lambda record: record.get("seen_at", ""), reverse=True)[:100]
            if rows:
                st.dataframe([
                    {
                        "platform": record.get("platform"), "author": record.get("author"),
                        "comment": record.get("text", "")[:300], "sentiment": record.get("sentiment"),
                        "category": record.get("category"), "reply_status": record.get("reply_status"),
                        "reply": record.get("reply_text"), "idea": record.get("idea"),
                        "idea_status": record.get("idea_status", ""), "published_at": record.get("published_at"),
                    }
                    for record in rows
                ], use_container_width=True, hide_index=True)
            else:
                st.caption(tr("Comment Agent No Comments"))
        with st.expander(tr("Comment Agent Video Ideas")):
            if ideas:
                st.dataframe([
                    {key: idea.get(key) for key in (
                        "idea", "score", "platform", "status", "stage", "subject",
                        "created_at", "finished_at", "request_id", "error",
                    )}
                    for idea in reversed(ideas[-50:])
                ], use_container_width=True, hide_index=True)
            else:
                st.caption(tr("Comment Agent No Ideas"))
