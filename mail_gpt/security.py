import re


def rejection(mail, config):
    if mail.sender == config.email or mail.sender not in config.allowed:
        return "sender"
    if config.email not in mail.recipients:
        return "recipient"
    msg = mail.headers
    if any(str(v).strip().lower() != "no" for v in msg.get_all("Auto-Submitted", [])):
        return "auto-submitted"
    if any(str(v).strip().lower() in {"bulk", "junk", "list", "auto_reply"} for v in msg.get_all("Precedence", [])):
        return "precedence"
    if any(str(v).strip().lower() not in {"", "none"} for v in msg.get_all("X-Auto-Response-Suppress", [])):
        return "auto-suppress"
    if any(h in msg for h in ("X-Mail-GPT", "List-Id", "X-Autoreply", "X-Autorespond")):
        return "automated"
    if "Return-Path" in msg and str(msg["Return-Path"]).strip() == "<>":
        return "bounce"
    if msg.get_content_type() == "multipart/report":
        return "report"
    if config.require_dmarc:
        # Gmail prepends its receiver-generated result. Never scan later, forgeable headers.
        results = msg.get_all("Authentication-Results", [])
        result = str(results[0]) if results else ""
        server, _, methods = result.partition(";")
        domain = mail.sender.rsplit("@", 1)[-1]
        dmarc = re.search(r"(?:^|;)\s*dmarc=pass\b([^;]*)", methods, re.I)
        aligned = re.search(r"\bheader\.from=([^\s;()]+)", dmarc[1], re.I) if dmarc else None
        if (server.strip().lower() != config.auth_serv_id.lower() or not aligned
                or aligned[1].strip('"').lower() != domain):
            return "authentication"
    return None
