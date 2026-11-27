"""NA-06.7.4.1: bounded read-only recovery; never replay a model write."""
from __future__ import annotations
import re
import time
from relay_logging import LOG

READ_DELAYS = (0, 2, 5, 10)
TURN_DELAYS = (0, 3, 5, 8, 12, 18, 25)
_STAGES = frozenset(("authority","discovery","resolution","retrieval","publish","verification"))
_REASONS = frozenset("""
DESTINATION_PARENT_UNAVAILABLE DESTINATION_SYMLINK_REJECTED DESTINATION_NOT_REGULAR_FILE
DESTINATION_EXISTS STAGING_CREATE_FAILED ARTIFACT_SIZE_LIMIT_EXCEEDED READ_TRANSPORT_FAILED
JSON_RESPONSE_INVALID FILES_RESPONSE_SHAPE_UNSUPPORTED ARTIFACT_NOT_FOUND ARTIFACT_SELECTOR_AMBIGUOUS
PRODUCT_FILE_ID_REQUIRED RESOLVER_RESPONSE_SHAPE_UNSUPPORTED RESOLVER_LOCATOR_MISSING LOCATOR_REJECTED
ATOMIC_PUBLISH_FAILED FILES_REQUEST_FAILED RESOLVER_REQUEST_FAILED LOCATOR_REDIRECT_NOT_FOLLOWED
LOCATOR_FETCH_FAILED STAGING_VERIFICATION_FAILED SOURCE_SIZE_MISMATCH POST_WRITE_INTEGRITY_UNAVAILABLE
POST_WRITE_INTEGRITY_MISMATCH
""".split())
_TEMP_HTTP = frozenset((408,425,429,500,502,503,504))
_MARKER = re.compile(r"\[AgentRelay recovery identity: [0-9a-f]{32}\]")


def wait_permitted(delay, permitted):
    deadline = time.monotonic() + delay
    while permitted():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(.25, remaining))
    return False


def read_error(exc):
    """Allowlisted diagnostics; no raw exception, body, path or URL."""
    stage, reason, http = (getattr(exc,k,None) for k in ("stage","reason","status_code"))
    stage = stage if isinstance(stage,str) and stage in _STAGES else "unknown"
    reason = reason if isinstance(reason,str) and reason in _REASONS else (
        "HOST_VALIDATION_FAILED" if isinstance(exc,ValueError) else "HANDOFF_FAILED")
    http = http if type(http) is int and 100 <= http <= 599 else None
    temporary = (stage == "discovery" and reason == "ARTIFACT_NOT_FOUND"
        or stage in ("discovery","resolution","retrieval") and (
            reason == "READ_TRANSPORT_FAILED" and (http is None or http == 200 or http in _TEMP_HTTP)
            or reason in ("FILES_REQUEST_FAILED","RESOLVER_REQUEST_FAILED","LOCATOR_FETCH_FAILED")
            and http in _TEMP_HTTP))
    return stage,reason,http,bool(temporary and getattr(exc,"destination_state",None) == "unchanged")


def recovery_eligible(exc):
    return (getattr(exc,"request_stage",None) in (
        "transport","transport_timeout","conversation_stream","canonical_final_text","conversation_fetch")
        and getattr(exc,"status_code",None) not in (401,403))


def _message(node):
    msg = node.get("message") if isinstance(node,dict) else None
    return msg if isinstance(msg,dict) else {}


def _role(msg):
    author = msg.get("author")
    return author.get("role") if isinstance(author,dict) else None


def _text(msg):
    content = msg.get("content")
    if not isinstance(content,dict) or content.get("content_type") != "text":
        return None
    parts = content.get("parts")
    return "".join(parts) if isinstance(parts,list) and parts and all(isinstance(p,str) for p in parts) else None


def exact_turn_text(snapshot, conversation_id, marker):
    """Unique marker, exact conversation, current visible final and nearest user."""
    if (not _MARKER.fullmatch(marker) or getattr(snapshot,"complete",None) is not True
            or getattr(snapshot,"conversation_id",None) != conversation_id):
        return None
    payload = snapshot.to_canonical_payload()
    if not isinstance(payload,dict) or payload.get("conversation_id") != conversation_id:
        return None
    mapping,current = payload.get("mapping"),payload.get("current_node")
    if (not isinstance(mapping,dict) or len(mapping)>100000 or not isinstance(current,str)
            or not current or getattr(snapshot,"current_node",None)!=current):
        return None
    matches = []
    for nid,node in mapping.items():
        msg = _message(node)
        text = _text(msg) if _role(msg)=="user" else None
        if text is not None and marker in text.splitlines():
            matches.append(nid)
            if len(matches)>1:
                return None
    if len(matches)!=1:
        return None
    final = _message(mapping.get(current))
    if (_role(final)!="assistant" or final.get("recipient")!="all"
            or final.get("channel") not in (None,"final")
            or final.get("status")!="finished_successfully" or final.get("end_turn") is not True
            or not isinstance(final.get("id"),str) or not final["id"]):
        return None
    meta=final.get("metadata")
    if isinstance(meta,dict) and (meta.get("is_visually_hidden_from_conversation") is True
                                  or meta.get("is_complete") is False):
        return None
    text=_text(final)
    if not text or not text.strip() or len(text)>100000:
        return None
    nid,seen=current,set()
    while isinstance(nid,str) and nid and nid not in seen:
        seen.add(nid)
        node=mapping.get(nid)
        if not isinstance(node,dict):
            return None
        msg=_message(node)
        if _role(msg)=="user":
            return text if nid==matches[0] and isinstance(msg.get("id"),str) and msg["id"] else None
        nid=node.get("parent")
    return None


def recover_turn(client, conversation_id, marker, *, permitted, delays=TURN_DELAYS, wait=wait_permitted):
    for attempt,delay in enumerate(delays,1):
        if not wait(delay,permitted) or not permitted():
            return None
        try:
            text=exact_turn_text(client.get_conversation_snapshot(conversation_id),conversation_id,marker)
        except Exception:
            text=None
        if not permitted():
            return None
        LOG.debug("Native recovery · canonical read=%s · confirmed=%s",attempt,bool(text))
        if text is not None:
            return text
    return None
