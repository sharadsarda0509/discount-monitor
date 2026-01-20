# Changelog

## 2026-01-20 - Major Fix: API-Based Monitoring

### Problem Identified
The script was failing with:
- ❌ "You need to enable JavaScript to run this app"
- ❌ BeautifulSoup deprecation warnings
- ❌ Unable to extract discount information

**Root Cause:** The OnePlay Store website is a JavaScript-rendered SPA (Single Page Application). The Python `requests` library only fetches static HTML, missing all dynamically loaded content.

### Solution Implemented
✅ **Switched from web scraping to direct API calls**

Instead of scraping HTML, the script now uses the same API endpoint the website uses:
```
POST https://commerce-services.oneplay.in/v1/content/details/info/{PRODUCT_ID}
```

### Changes Made

1. **New API-based approach**
   - Direct JSON API calls for reliable data
   - No JavaScript rendering required
   - Faster execution (no HTML parsing)

2. **Removed dependencies**
   - Removed `beautifulsoup4` (no longer needed)
   - Removed `lxml` (no longer needed)
   - Only `requests` library required now

3. **Improved data extraction**
   - Product name, prices, and discount from API response
   - `response.info.discount_percentage` - direct discount value
   - `response.info.best_buy_price` - current price
   - `response.info.best_display_price` - original price

4. **Better error handling**
   - Clear logging at each step
   - Proper JSON parsing with fallbacks
   - Detailed error messages

### Test Results (Local)

```
============================================================
Flipkart Discount Monitor - Run at 2026-01-20 20:26:03
============================================================
[2026-01-20 20:26:03] Fetching product data from API...
[2026-01-20 20:26:03] Successfully fetched data (Status: 200)
[2026-01-20 20:26:03] Product: Flipkart E-Gift Voucher INR 10000
[2026-01-20 20:26:03] Current Price: ₹9900
[2026-01-20 20:26:03] Original Price: ₹10000.0
[2026-01-20 20:26:03] Discount: 1.0%
[2026-01-20 20:26:03] Current discount: 1.0% | Target: 2.0%
[2026-01-20 20:26:03] Target not reached yet. Waiting...
```

✅ **Working perfectly!**

### What to Expect in GitHub Actions

When you trigger the workflow now, you should see:
- ✅ No deprecation warnings
- ✅ Successful API calls (Status: 200)
- ✅ Product name and prices extracted correctly
- ✅ Discount percentage displayed
- ✅ Comparison with target (currently 1% vs 2% target)

### Next Steps

The automation is ready! Just ensure your GitHub secrets are configured:
1. `SENDER_EMAIL` - Your Gmail
2. `RECEIVER_EMAIL` - Alert destination email
3. `EMAIL_PASSWORD` - Gmail App Password

The workflow will now:
- Run automatically every 30 minutes
- Fetch discount data via API (fast & reliable)
- Send email when discount reaches 2%+

### Benefits of API Approach

| Before (HTML Scraping) | After (API Calls) |
|------------------------|-------------------|
| ❌ Requires JavaScript engine | ✅ Direct data access |
| ❌ Slow (HTML parsing) | ✅ Fast (JSON parsing) |
| ❌ Breaks if HTML changes | ✅ Stable API contract |
| ❌ Heavy dependencies | ✅ Minimal dependencies |
| ❌ Deprecation warnings | ✅ Clean output |

---

**Status: FIXED AND DEPLOYED** ✅

