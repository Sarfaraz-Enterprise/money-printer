"""Private profile controls. Secrets are never prefilled or placed in result tables."""

from datetime import datetime

import streamlit as st
import pytz
from filelock import Timeout

from app.services import accounts, scheduler, voice


def render_account_targets(key: str) -> list[str]:
    try:
        profiles = accounts.list_profiles()
    except (ValueError, OSError, Timeout):
        st.error("Cannot load publishing profiles. Check private account storage.")
        st.stop()
        return []
    names = {profile["id"]: profile["name"] for profile in profiles}
    selected = st.multiselect(
        "Target Accounts / Channels", list(names),
        format_func=lambda value: names[value], key=key,
        help="Selecting profiles authorizes publishing to their enabled destinations after rendering. Empty selection retains the legacy Upload-Post behavior.",
    )
    return selected


def _profile_form(profile: dict | None = None) -> None:
    profile = profile or {}
    identifier = profile.get("id", "new")
    schedule = profile.get("schedule", {})
    times = schedule.get("post_times_ist", schedule.get("post_times", schedule.get("slots", ["05:00", "20:00"])))
    count = st.number_input("Number of Daily Posting Times", min_value=1, max_value=1440,
                            value=len(times), key=f"account-time-count-{identifier}")
    with st.form(f"account-edit-{identifier}"):
        custom_id = st.text_input("Profile ID (optional; immutable after saving)", value=profile.get("id", ""),
                                  disabled=bool(profile.get("id")), key=f"account-id-{identifier}",
                                  help="Lowercase letters, numbers, underscores or hyphens. Blank creates a unique ID.")
        name = st.text_input("Profile Name", value=profile.get("name", ""), key=f"account-name-{identifier}")
        niche = st.text_input("Category / Niche", value=profile.get("category", profile.get("niche", "General")), key=f"account-niche-{identifier}")
        audience = st.text_input("Target Audience", value=profile.get("target_audience", "United States (US Tier-1)"), key=f"account-audience-{identifier}")
        audience_tags = st.text_input("Audience Tags (comma-separated)", value=", ".join(profile.get("audience_tags", [])), key=f"account-tags-{identifier}")
        selected_zone = profile.get("timezone", "America/New_York")
        audience_zones = list(dict.fromkeys([*pytz.common_timezones, selected_zone]))
        audience_zone = st.selectbox("Audience Timezone", audience_zones,
                                    index=audience_zones.index(selected_zone),
                                    key=f"account-audience-zone-{identifier}")
        st.caption("Audience timezone guides creative localization. Posting times below always use IST.")
        niche_prompt = st.text_area("System Prompt / Creative Direction", value=profile.get("system_prompt", profile.get("niche_prompt", "")), key=f"account-prompt-{identifier}")
        visual_style = st.text_input("Visual Style Keywords", value=profile.get("visual_keywords_preset", profile.get("visual_style", "")), key=f"account-visual-{identifier}",
                                     placeholder="Dark cinematic hallway, rain at night, dramatic shadows")
        voices = list(dict.fromkeys([voice.ELEVENLABS_SERVER_DEFAULT_VOICE, *voice.get_elevenlabs_catalog_voices(), profile.get("voice_name") or voice.ELEVENLABS_SERVER_DEFAULT_VOICE]))
        profile_voice = st.selectbox("ElevenLabs Narrator", voices, index=voices.index(profile.get("voice_name") or voice.ELEVENLABS_SERVER_DEFAULT_VOICE), key=f"account-voice-{identifier}")
        voice_direction = st.text_area("Voice / Pacing Direction", value=profile.get("voice_direction", "Atmospheric American English, controlled suspense and natural pauses"), key=f"account-direction-{identifier}")
        platforms = ["youtube", "instagram", "facebook"]
        destinations = st.multiselect("Authorized Destinations", platforms,
                                      default=[name for name in platforms if profile.get(name, {}).get("enabled")],
                                      key=f"account-destinations-{identifier}")
        page_id = st.text_input("Facebook Page ID", value=profile.get("facebook", {}).get("page_id", ""), key=f"account-page-{identifier}")
        token = st.text_input("Page Access Token (blank keeps saved token)", type="password", key=f"account-token-{identifier}")
        business_id = st.text_input("Instagram Business Account ID", value=profile.get("instagram", {}).get("business_account_id", ""), key=f"account-business-{identifier}")
        st.subheader("Daily Posting Schedule (IST)")
        scheduled = st.toggle("Enable this profile's daily schedule", value=schedule.get("enabled", False),
                              key=f"account-scheduled-{identifier}")
        post_times = [
            st.time_input(f"Posting Time {index + 1} (IST)",
                          value=datetime.strptime(times[index] if index < len(times) else f"{index // 60:02d}:{index % 60:02d}", "%H:%M").time(),
                          step=60, key=f"account-time-{identifier}-{index}").strftime("%H:%M")
            for index in range(int(count))
        ]
        st.caption("Times belong only to this profile. The master agent must be Active to publish. Blank tokens preserve saved credentials.")
        if st.form_submit_button("Save Profile"):
            payload = {
                "name": name, "category": niche, "system_prompt": niche_prompt, "visual_keywords_preset": visual_style, "enabled": True,
                "target_audience": audience, "audience_tags": [tag.strip() for tag in audience_tags.split(",") if tag.strip()],
                "timezone": audience_zone, "voice_name": profile_voice, "voice_direction": voice_direction,
                "schedule": {**schedule, "enabled": scheduled, "timezone": "Asia/Kolkata", "post_times_ist": post_times},
                "destinations": {name: name in destinations for name in platforms},
                "facebook": {"page_id": page_id, "page_access_token": token},
                "instagram": {"business_account_id": business_id},
            }
            if profile.get("id"):
                payload["id"] = profile["id"]
            elif custom_id.strip():
                payload["id"] = custom_id.strip()
            try:
                if len(set(post_times)) != len(post_times):
                    raise ValueError("Duplicate profile posting times")
                accounts.save_profile(payload)
            except (ValueError, OSError):
                st.toast("🔴 Error: Profile not saved. Check times, fields and storage permissions.")
            else:
                st.toast("Profile saved")
                st.rerun()


def render_accounts_manager() -> None:
    st.subheader("Accounts Manager")
    st.caption("Per-profile schedules are independent of the global Auto-Pilot switch. Local status indicates saved credentials; Test connections checks live API access.")
    try:
        profiles = accounts.list_profiles()
    except (ValueError, OSError):
        st.error("Account storage is invalid or inaccessible.")
        return
    for profile in profiles:
        identifier = profile["id"]
        with st.container(border=True):
            st.subheader(profile["name"])
            st.caption(profile["niche"])
            schedule = profile.get("schedule") or {}
            st.caption(
                "Schedule: " + ", ".join(schedule.get("slots", []))
                + " " + schedule.get("timezone", "Asia/Kolkata")
                if schedule.get("enabled") else "Account schedule disabled"
            )
            columns = st.columns(3)
            for column, platform in zip(columns, ("youtube", "instagram", "facebook")):
                state = profile.get(platform, {})
                column.write(f"{platform.title()}: " + ("Connected" if state.get("connected") else "Disconnected"))
            if st.button("Test connections", key=f"account-test-{identifier}"):
                try:
                    st.json(accounts.validate_profile(identifier))
                except (ValueError, OSError):
                    st.error("Connection check could not complete.")
            if st.button("Connect YouTube", key=f"account-oauth-{identifier}"):
                try:
                    url = accounts.start_youtube_oauth(identifier)
                except (ValueError, OSError):
                    st.error("Cannot start OAuth. Check GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET and localhost callback availability.")
                else:
                    st.session_state[f"account-oauth-url-{identifier}"] = url
            if url := st.session_state.get(f"account-oauth-url-{identifier}"):
                st.link_button("Authorize this YouTube channel", url)
                st.caption("Open on the server's desktop. OAuth waits for the localhost callback; each profile has a separate token.")
                if st.button("Refresh OAuth status", key=f"account-oauth-refresh-{identifier}"):
                    st.json(accounts.oauth_status(identifier))
            with st.expander("Edit Profile"):
                _profile_form(profile)
            confirm = st.checkbox("Confirm delete profile and its local YouTube token", key=f"account-delete-confirm-{identifier}")
            if st.button("Delete Profile", key=f"account-delete-{identifier}", disabled=not confirm):
                try:
                    accounts.delete_profile(identifier)
                except (ValueError, OSError):
                    st.error("Profile could not be deleted.")
                else:
                    st.rerun()
    with st.expander("Add New Profile"):
        _profile_form()
    with st.expander("Recent Account Publishing Results"):
        from app.services.account_dispatcher import recent_dispatches
        st.button("Refresh publishing status", key="account-dispatch-refresh")
        try:
            rows = recent_dispatches()
        except (ValueError, OSError, Timeout):
            st.error("Publishing history is inaccessible.")
        else:
            if rows:
                st.dataframe(rows, hide_index=True, use_container_width=True)
            else:
                st.caption("No account publishing runs yet.")
