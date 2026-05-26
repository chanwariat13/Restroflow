"""
routes/customer_pages.py
Customer-facing pages — fully branded per client.
  GET /r/{slug}?table=T1&secret=ABC   → Registration page
  GET /menu/{slug}?t=T1&p=91xx&k=tok → Menu page

Security note
-------------
Branding fields (restaurant_name, welcome_message, primary_color, logo_url,
banner_image) come from the master DB and are settable by per-tenant
admins. RestroFlow is multi-tenant: a malicious or compromised tenant
operator must not be able to inject script into another customer's
browser. Every value is therefore sanitised through one of the helpers
below before being interpolated into the customer-facing HTML/CSS/JS.
"""
import html
import json
import os
import re
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from app.models.database import MasterSession, Client

router = APIRouter()


# ── Branding sanitisation helpers ───────────────────────────────────────────
# Hex colours: 3, 4, 6 or 8 hex digits (covers #rgb, #rgba, #rrggbb, #rrggbbaa).
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def _safe_color(value: str, default: str = "#ff6b35") -> str:
    """Return a CSS-safe hex colour, or the default when the input doesn't match.

    The previous code interpolated `primary_color` directly into CSS, which
    let an admin-controlled value escape the rule (`red;}body{display:none`)
    or even break out of `<style>` (`</style><script>…`). Validating against
    a strict hex pattern eliminates both vectors.
    """
    v = (value or "").strip()
    return v if _HEX_COLOR_RE.match(v) else default


def _safe_url(value: str) -> str:
    """Return an attribute-safe URL, or '' for unsafe / non-URL inputs.

    Allows http(s):// URLs and absolute/relative paths. Disallows
    `javascript:`, `data:`, `file:` etc., which are XSS vectors when
    interpolated into `<img src>`.
    """
    v = (value or "").strip()
    if not v:
        return ""
    lower = v.lower()
    if lower.startswith(("http://", "https://")) or v.startswith(("/", "./")):
        return html.escape(v, quote=True)
    return ""


def _h(value: str) -> str:
    """HTML-escape (including quotes) — safe for both text nodes and attributes."""
    return html.escape(value or "", quote=True)


def _jsstr(value: str) -> str:
    """Encode `value` as a complete JS string literal (with quotes) — safe to
    drop into a <script> block without further escaping."""
    return json.dumps(value or "")


def _get_client(slug: str) -> Client | None:
    db = MasterSession()
    try:
        return db.query(Client).filter(Client.slug == slug, Client.active == True).first()
    finally:
        db.close()


def _css_vars(color: str) -> str:
    return f"--accent:{color};--accent2:{color}cc;--accent-light:{color}22;"


@router.get("/r/{slug}", response_class=HTMLResponse)
async def registration_page(slug: str):
    c = _get_client(slug)
    # Sanitise every tenant-controlled field before interpolation.
    name_raw    = c.restaurant_name if c else "Restaurant"
    color       = _safe_color(c.primary_color if c else "#ff6b35", "#ff6b35")
    logo_url    = _safe_url(c.logo_url if c else "")
    welcome_raw = (c.welcome_message if c else "Welcome! Scan & Order") or "Welcome! Scan & Order"
    banner_url  = _safe_url(c.banner_image if c else "")

    name      = _h(name_raw)
    welcome   = _h(welcome_raw)
    slug_js   = _jsstr(slug)

    logo_html = (
        f'<img src="{logo_url}" alt="{name}" '
        f'style="height:60px;object-fit:contain;margin-bottom:8px;border-radius:10px">'
        if logo_url
        else f'<div style="font-family:Syne,sans-serif;font-size:28px;font-weight:800;color:{color}">{name}</div>'
    )
    banner_html = (
        f'<img src="{banner_url}" alt="banner" '
        f'style="width:100%;height:140px;object-fit:cover;border-radius:16px 16px 0 0">'
        if banner_url
        else ""
    )

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>{name} — Scan & Order</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{ {_css_vars(color)} }}
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:'DM Sans',sans-serif;background:#0a0a0f;color:#f0f0f5;min-height:100vh;
    display:flex;align-items:center;justify-content:center;padding:20px;
    background:radial-gradient(ellipse at 50% 0%,{color}15 0%,#0a0a0f 70%)}}
  .card{{background:#13131a;border:1px solid #2a2a38;border-radius:20px;width:100%;
    max-width:400px;overflow:hidden;box-shadow:0 0 60px {color}18}}
  .card-body{{padding:28px 28px 32px}}
  .logo-area{{text-align:center;margin-bottom:6px}}
  .restro-name{{font-family:'Syne',sans-serif;font-size:22px;font-weight:800;color:{color};text-align:center;margin-bottom:4px}}
  .welcome{{text-align:center;color:#8888a0;font-size:13px;margin-bottom:24px}}
  .table-badge{{background:{color}18;border:1px solid {color}44;color:{color};border-radius:12px;
    padding:10px 16px;font-size:14px;font-weight:600;text-align:center;margin-bottom:20px;display:none}}
  label{{display:block;font-size:12px;color:#8888a0;margin-bottom:6px;font-weight:500}}
  input{{width:100%;background:#1c1c26;border:1px solid #2a2a38;border-radius:12px;
    padding:13px 16px;color:#f0f0f5;font-size:15px;font-family:'DM Sans',sans-serif;
    outline:none;margin-bottom:16px;transition:border-color 0.2s}}
  input:focus{{border-color:{color}}}
  .btn{{width:100%;background:linear-gradient(135deg,{color},{color}aa);border:none;
    border-radius:12px;padding:14px;color:white;font-size:15px;font-weight:600;
    font-family:'DM Sans',sans-serif;cursor:pointer;transition:opacity 0.2s}}
  .btn:hover{{opacity:0.9}}
  .btn:disabled{{opacity:0.5;cursor:not-allowed}}
  .msg{{border-radius:10px;padding:12px 16px;font-size:14px;margin-top:14px;text-align:center;display:none}}
  .msg.success{{background:#06d6a018;border:1px solid #06d6a044;color:#06d6a0}}
  .msg.error{{background:#ef444418;border:1px solid #ef444444;color:#f87171}}
  .note{{text-align:center;font-size:12px;color:#8888a0;margin-top:16px}}
  hr{{border:none;border-top:1px solid #2a2a38;margin:18px 0}}
</style>
</head>
<body>
<div class="card">
  {banner_html}
  <div class="card-body">
    <div class="logo-area">{logo_html}</div>
    <div class="restro-name">{name}</div>
    <div class="welcome">{welcome}</div>
    <div class="table-badge" id="table-badge">🪑 Table: <span id="table-display"></span></div>
    <input type="hidden" id="table-val">
    <input type="hidden" id="secret-val">
    <div>
      <label>Your Name</label>
      <input type="text" id="inp-name" placeholder="Enter your full name" maxlength="30">
      <label>WhatsApp Number</label>
      <input type="tel" id="inp-phone" placeholder="91XXXXXXXXXX (with country code)" maxlength="15">
      <div id="welcome-back" style="display:none;background:{color}18;border:1px solid {color}44;color:{color};border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:14px;text-align:center"></div>
      <button class="btn" id="reg-btn" onclick="register()">Send Request →</button>
      <div class="msg" id="msg"></div>
    </div>
    <hr>
    <div class="note">Staff will approve your request on WhatsApp 🙏</div>
  </div>
</div>
<script>
  const BASE=window.location.origin, SLUG={slug_js};
  const params=new URLSearchParams(window.location.search);
  const table=params.get('table')||'', secret=params.get('secret')||'';
  if(table){{
    document.getElementById('table-badge').style.display='block';
    document.getElementById('table-display').textContent=table;
    document.getElementById('table-val').value=table;
    document.getElementById('secret-val').value=secret;
  }}
  async function register(){{
    const name=document.getElementById('inp-name').value.trim();
    const phone=document.getElementById('inp-phone').value.trim();
    const table=document.getElementById('table-val').value;
    const secret=document.getElementById('secret-val').value;
    const btn=document.getElementById('reg-btn');
    if(!name||name.length<2){{showMsg('Please enter your name','error');return}}
    if(!phone||phone.length<10){{showMsg('Enter valid WhatsApp number with country code','error');return}}
    if(!table){{showMsg('Invalid QR code. Please scan again.','error');return}}
    btn.disabled=true;btn.textContent='Sending...';
    try{{
      const r=await fetch(BASE+'/webhook/'+SLUG+'/register',{{
        method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{phone,name,table,secret}})
      }});
      const d=await r.json();
      if(d.success){{showMsg('✅ '+d.message,'success');btn.textContent='Request Sent!'}}
      else{{showMsg('❌ '+d.error,'error');btn.disabled=false;btn.textContent='Send Request →'}}
    }}catch(e){{showMsg('Network error. Try again.','error');btn.disabled=false;btn.textContent='Send Request →'}}
  }}
  function showMsg(text,type){{
    const el=document.getElementById('msg');
    el.textContent=text;el.className='msg '+type;el.style.display='block';
  }}
  document.getElementById('inp-phone').addEventListener('keydown',e=>{{if(e.key==='Enter')register()}});

  // ── Returning-customer auto-fill ──────────────────────────────────────
  // When the phone number reaches 10+ digits, ping the server. If the guest has
  // visited before, pre-fill the name field and show a friendly "Welcome back!"
  let _lookupTimer=null, _lastLookup='';
  document.getElementById('inp-phone').addEventListener('input', function() {{
    const phone=this.value.trim().replace(/\\D/g,'');
    if(phone.length<10 || phone===_lastLookup) return;
    clearTimeout(_lookupTimer);
    _lookupTimer=setTimeout(async()=>{{
      _lastLookup=phone;
      try{{
        const r=await fetch(BASE+'/webhook/'+SLUG+'/lookup-customer?phone='+encodeURIComponent(phone));
        const d=await r.json();
        const wb=document.getElementById('welcome-back');
        const ni=document.getElementById('inp-name');
        if(d && d.found && d.name){{
          if(!ni.value || ni.value.trim().length<2) ni.value=d.name;
          wb.textContent='👋 Welcome back, '+d.name+'! ('+d.visits+' visits)';
          wb.style.display='block';
        }} else {{
          wb.style.display='none';
        }}
      }}catch(e){{ /* silent */ }}
    }}, 350);
  }});
</script>
</body>
</html>""")


@router.get("/menu/{slug}", response_class=HTMLResponse)
async def menu_page(slug: str):
    c = _get_client(slug)
    name_raw    = c.restaurant_name if c else "Restaurant"
    color       = _safe_color(c.primary_color if c else "#ff6b35", "#ff6b35")
    logo_url    = _safe_url(c.logo_url if c else "")
    welcome_raw = (c.welcome_message if c else "") or ""

    name    = _h(name_raw)
    welcome = _h(welcome_raw)
    slug_h  = _h(slug)

    # When no logo is configured, render an empty slot — the .restro-name
    # element next to the slot already shows the restaurant name, so we
    # would otherwise display the name twice. The .header-brand wrapper
    # in menu.html collapses gracefully when the slot is empty.
    logo_html = (
        f'<img src="{logo_url}" alt="{name}" '
        f'style="height:40px;object-fit:contain;border-radius:8px">'
        if logo_url else ""
    )

    # Read the menu HTML template and inject branding. Note that we replace
    # the literal "#ff6b35" hex-string used as the default in the template
    # with the validated tenant colour. That replacement is only safe
    # because `color` has been run through _safe_color() above.
    path = os.path.join(os.path.dirname(__file__), "..", "..", "static", "menu.html")
    try:
        with open(path) as f:
            content = f.read()
        content = (content
            .replace("__SLUG__", slug_h)
            .replace("__RESTRO_NAME__", name)
            .replace("__COLOR__", color)
            .replace("__LOGO_HTML__", logo_html)
            .replace("__WELCOME__", welcome)
            .replace("#ff6b35", color))
        return HTMLResponse(content)
    except FileNotFoundError:
        return HTMLResponse(f"<h2>{name} Menu</h2><p>Menu page coming soon.</p>")
