"""Ready-made custom alert templates (section 6 · Custom Alert).

Like the message templates on a DASDEC / Sage front panel: pick one, and the
event code, originator, severity, valid time and a standard message are
filled in. Anything typed in the Event / Message / Instructions / From fields
still overrides the template. Text is deliberately generic -- edit the
message for the actual situation.
"""

__all__ = ["CUSTOM_TEMPLATES", "TEMPLATE_CHOICES", "apply_template"]

CUSTOM_TEMPLATES = {
    "custom": None,
    "cem": {
        "label": "Civil Emergency Message (CEM)", "event": "Civil Emergency Message",
        "code": "CEM", "org": "CIV", "severity": "Severe", "minutes": 60,
        "sender": "Local Emergency Management",
        "message": "Local officials have declared a civil emergency for the area listed. "
                   "Stay tuned to local media for further information.",
        "instruction": "Follow the instructions of local officials.",
    },
    "lae": {
        "label": "Local Area Emergency (LAE)", "event": "Local Area Emergency",
        "code": "LAE", "org": "CIV", "severity": "Moderate", "minutes": 60,
        "sender": "Local Emergency Management",
        "message": "An emergency affecting the area listed is in progress.",
        "instruction": "Avoid the area and follow the directions of public safety officials.",
    },
    "spw": {
        "label": "Shelter in Place Warning (SPW)", "event": "Shelter in Place Warning",
        "code": "SPW", "org": "CIV", "severity": "Extreme", "minutes": 60,
        "sender": "Local Emergency Management",
        "message": "A shelter in place order is in effect for the area listed.",
        "instruction": "Go indoors immediately. Close all windows and doors and turn off "
                       "ventilation systems. Stay inside until officials say it is safe.",
    },
    "evi": {
        "label": "Evacuation Immediate (EVI)", "event": "Evacuation Immediate",
        "code": "EVI", "org": "CIV", "severity": "Extreme", "minutes": 120,
        "sender": "Local Emergency Management",
        "message": "An immediate evacuation has been ordered for the area listed.",
        "instruction": "Leave the area now using the routes given by local officials. "
                       "Take essential medications and important documents.",
    },
    "hmw": {
        "label": "Hazardous Materials Warning (HMW)", "event": "Hazardous Materials Warning",
        "code": "HMW", "org": "CIV", "severity": "Severe", "minutes": 120,
        "sender": "Local Fire Department",
        "message": "A hazardous materials release has occurred in the area listed.",
        "instruction": "Avoid the area. If you are near the release, go indoors and close "
                       "windows and doors.",
    },
    "cdw": {
        "label": "Civil Danger Warning (CDW)", "event": "Civil Danger Warning",
        "code": "CDW", "org": "CIV", "severity": "Severe", "minutes": 60,
        "sender": "Local Emergency Management",
        "message": "A dangerous situation is occurring in the area listed.",
        "instruction": "Take protective action and stay tuned for further information.",
    },
    "lew": {
        "label": "Law Enforcement Warning (LEW)", "event": "Law Enforcement Warning",
        "code": "LEW", "org": "CIV", "severity": "Severe", "minutes": 60,
        "sender": "Local Police Department",
        "message": "Law enforcement is responding to an incident in the area listed.",
        "instruction": "Avoid the area and follow the directions of law enforcement.",
    },
    "cae": {
        "label": "Child Abduction Emergency / AMBER Alert (CAE)", "event": "Child Abduction Emergency",
        "code": "CAE", "org": "CIV", "severity": "Severe", "minutes": 300,
        "sender": "State Police",
        "message": "Law enforcement has issued an AMBER Alert for an abducted child. "
                   "Edit this message with the child's and suspect's description.",
        "instruction": "If you see the child or the suspect vehicle, call 911 immediately. "
                       "Do not approach.",
    },
    "frw": {
        "label": "Fire Warning (FRW)", "event": "Fire Warning",
        "code": "FRW", "org": "CIV", "severity": "Severe", "minutes": 120,
        "sender": "Local Fire Department",
        "message": "A spreading fire threatens the area listed.",
        "instruction": "Be ready to leave immediately if told to evacuate.",
    },
    "toe": {
        "label": "911 Telephone Outage Emergency (TOE)", "event": "911 Telephone Outage Emergency",
        "code": "TOE", "org": "CIV", "severity": "Moderate", "minutes": 240,
        "sender": "Local Emergency Communications",
        "message": "911 service is currently unavailable in the area listed.",
        "instruction": "In an emergency, call the alternate number given by local officials "
                       "or go to the nearest fire or police station.",
    },
    "boil": {
        "label": "Boil Water Notice (Administrative Message, ADR)", "event": "Administrative Message",
        "code": "ADR", "org": "CIV", "severity": "Minor", "minutes": 720,
        "sender": "Water Utility",
        "message": "A boil water notice is in effect for the area listed.",
        "instruction": "Boil tap water for at least one minute before drinking or cooking "
                       "until further notice.",
    },
    "adr": {
        "label": "Administrative Message (ADR)", "event": "Administrative Message",
        "code": "ADR", "org": "EAS", "severity": "Minor", "minutes": 60,
        "sender": "EAS Participant",
        "message": "This is an administrative message.",
        "instruction": "",
    },
    "dmo": {
        "label": "Practice / Demo Warning (DMO)", "event": "Practice/Demo Warning",
        "code": "DMO", "org": "EAS", "severity": "Minor", "minutes": 15,
        "sender": "EAS Participant",
        "message": "This is a practice demonstration of the Emergency Alert System. "
                   "This is only a test.",
        "instruction": "No action is required.",
    },
    "npt": {
        "label": "Nationwide Test (NPT)", "event": "National Periodic Test",
        "code": "NPT", "org": "PEP", "severity": "Minor", "minutes": 30,
        "sender": "Federal Emergency Management Agency",
        "message": "", "instruction": "",
    },
}

TEMPLATE_CHOICES = [("custom", "None — use exactly what is typed below")] + [
    (k, v["label"]) for k, v in CUSTOM_TEMPLATES.items() if v]


def apply_template(key, event="", message="", instruction="", sender="", severity="",
                   minutes=None):
    """Merge typed fields over the template. Returns a dict with event, code,
    org, severity, minutes, sender, message, instruction."""
    t = CUSTOM_TEMPLATES.get((key or "custom").lower()) or {}
    return {
        "event": (event or "").strip() or t.get("event", ""),
        "code": t.get("code", ""),
        "org": t.get("org", ""),
        "severity": severity or t.get("severity", "Severe"),
        "minutes": minutes or t.get("minutes", 30),
        "sender": (sender or "").strip() or t.get("sender", "EAS Participant"),
        "message": (message or "").strip() or t.get("message", ""),
        "instruction": (instruction or "").strip() or t.get("instruction", ""),
    }
