# Quick Setup Guide 🚀

Follow these steps to get your Flipkart Discount Monitor running on GitHub.

## Part 1: Get Gmail App Password (5 minutes)

1. **Go to Google Account Security**
   - Visit: https://myaccount.google.com/security

2. **Enable 2-Step Verification** (if not already enabled)
   - Scroll to "How you sign in to Google"
   - Click "2-Step Verification" → Follow the prompts

3. **Generate App Password**
   - Visit: https://myaccount.google.com/apppasswords
   - Select app: **Mail**
   - Select device: **Other** → Type "Flipkart Monitor"
   - Click **Generate**
   - **COPY the 16-character password** (you'll need this later)
   - Example format: `abcd efgh ijkl mnop`

## Part 2: Push to GitHub (5 minutes)

### Option A: Create New Repository via GitHub Website

1. Go to https://github.com/new
2. Repository name: `flipkart-discount-monitor` (or any name you like)
3. Choose **Private** (recommended)
4. **DO NOT** initialize with README (we already have one)
5. Click **Create repository**
6. On your computer, navigate to this folder:
   ```bash
   cd /Users/rismehta/af2-web-runtime/flipkart-discount-monitor
   ```
7. Run these commands (replace `YOUR_USERNAME` with your GitHub username):
   ```bash
   git init
   git add .
   git commit -m "Initial commit: Flipkart discount monitor"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/flipkart-discount-monitor.git
   git push -u origin main
   ```

### Option B: Use GitHub CLI (if installed)

```bash
cd /Users/rismehta/af2-web-runtime/flipkart-discount-monitor
gh repo create flipkart-discount-monitor --private --source=. --push
```

## Part 3: Configure GitHub Secrets (3 minutes)

1. Go to your repository on GitHub
2. Click **Settings** (top right)
3. In left sidebar: **Secrets and variables** → **Actions**
4. Click **New repository secret** (green button)

Add these 3 secrets one by one:

### Secret 1: SENDER_EMAIL
- Name: `SENDER_EMAIL`
- Value: `your-email@gmail.com`
- Click **Add secret**

### Secret 2: RECEIVER_EMAIL
- Name: `RECEIVER_EMAIL`
- Value: `your-email@gmail.com` (can be same as sender)
- Click **Add secret**

### Secret 3: EMAIL_PASSWORD
- Name: `EMAIL_PASSWORD`
- Value: `[paste the 16-char app password from Part 1]`
- Click **Add secret**

## Part 4: Enable & Test (2 minutes)

1. Go to **Actions** tab in your repository
2. If you see a message, click **"I understand my workflows, go ahead and enable them"**
3. Click on **"Flipkart Discount Monitor"** workflow (left sidebar)
4. Click **"Run workflow"** (right side) → **"Run workflow"** button
5. Wait 30-60 seconds
6. Click on the running workflow to see logs
7. Expand **"Check Flipkart discount"** step to see output

Expected output:
```
============================================================
Flipkart Discount Monitor - Run at 2026-01-20 15:30:00
============================================================
[2026-01-20 15:30:00] Fetching URL: https://...
[2026-01-20 15:30:02] Successfully fetched page (Status: 200)
[2026-01-20 15:30:02] Found discount: 1%
[2026-01-20 15:30:02] Current discount: 1% | Target: 2%
[2026-01-20 15:30:02] Target not reached yet. Waiting...
```

## ✅ You're Done!

The workflow will now run automatically every 30 minutes. When the discount reaches 2%, you'll get an email!

## 📧 Test Email (Optional)

To force send a test email immediately:

1. Edit `check_discount.py` in GitHub
2. Change line 19:
   ```python
   TARGET_DISCOUNT = 0.5  # Temporarily set to 0.5 to trigger email
   ```
3. Commit the change
4. Wait for the next workflow run or trigger manually
5. Check your email!
6. Remember to change it back to `2.0` after testing

## 🔧 Customization

### Change Check Frequency

Edit `.github/workflows/discount-checker.yml`:

```yaml
schedule:
  - cron: '*/30 * * * *'  # Every 30 mins
  # Change to:
  - cron: '0 * * * *'     # Every hour
  # Or:
  - cron: '0 */3 * * *'   # Every 3 hours
```

### Monitor Different Voucher

Edit `check_discount.py`, line 18:
```python
URL = "https://store.oneplay.in/view/YOUR-DIFFERENT-VOUCHER-URL"
```

### Change Target Discount

Edit `check_discount.py`, line 19:
```python
TARGET_DISCOUNT = 5.0  # Change to any percentage
```

## 🆘 Troubleshooting

| Problem | Solution |
|---------|----------|
| "Authentication failed" | Regenerate Gmail App Password, update GitHub Secret |
| Not receiving emails | Check spam folder, verify secrets are correct |
| Workflow not running | Enable Actions in repo settings, check `.github/workflows/` path |
| "Module not found" | Ensure `requirements.txt` is committed and pushed |

## 📊 Monitoring

Check workflow runs:
- **Actions** tab → **All workflows** → Select any run → View logs

View workflow history:
- See all past checks and their results
- Identify any failures or issues

## 💡 Tips

- Keep repository **private** to protect your email
- Use a dedicated Gmail account for sending alerts
- Check logs occasionally to ensure everything works
- GitHub Actions is free for private repos (2,000 mins/month)

---

**Need help?** Check the main [README.md](README.md) for detailed documentation.

