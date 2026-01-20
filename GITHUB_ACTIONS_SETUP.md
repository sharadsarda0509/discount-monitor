# GitHub Actions Setup Guide

## ✅ What's Already Done

The workflow file is configured and will run automatically **every 30 minutes** once you complete the setup below.

## 🔧 Required Steps to Activate Automation

### Step 1: Configure Gmail App Password

You need an App Password to send email alerts:

1. Go to your Google Account: https://myaccount.google.com/
2. Navigate to **Security**
3. Enable **2-Step Verification** (if not already enabled)
4. Go to **App Passwords**: https://myaccount.google.com/apppasswords
5. Select **Mail** and **Other (Custom name)** → Enter "Flipkart Monitor"
6. Click **Generate**
7. **Copy the 16-character password** (you'll need it in Step 2)

### Step 2: Add GitHub Secrets

Go to your repository: https://github.com/sharadsarda0509/discount-monitor

1. Click **Settings** (top menu)
2. Click **Secrets and variables** → **Actions** (left sidebar)
3. Click **New repository secret** button
4. Add these **3 secrets** one by one:

| Secret Name | Value | Example |
|-------------|-------|---------|
| `SENDER_EMAIL` | Your Gmail address | `yourname@gmail.com` |
| `RECEIVER_EMAIL` | Email to receive alerts | `yourname@gmail.com` (can be same) |
| `EMAIL_PASSWORD` | 16-char app password from Step 1 | `abcd efgh ijkl mnop` |

**Important:** 
- Use the **App Password** from Step 1, NOT your regular Gmail password
- Secret names must match exactly (case-sensitive)
- Each secret is added separately using the "New repository secret" button

### Step 3: Enable GitHub Actions

1. Go to the **Actions** tab in your repository
2. If you see a message about workflows, click **"I understand my workflows, go ahead and enable them"**
3. You should see "Flipkart Discount Monitor" workflow listed

### Step 4: Verify It's Working

#### Manual Test (Recommended First):
1. Go to **Actions** tab
2. Click **"Flipkart Discount Monitor"** workflow (left sidebar)
3. Click **"Run workflow"** dropdown (right side)
4. Click green **"Run workflow"** button
5. Wait 30-60 seconds, then refresh the page
6. Click on the workflow run to see logs
7. Expand **"Check Flipkart discount"** step to see detailed output

#### Check Automatic Runs:
- The workflow will run automatically every 30 minutes
- You'll see new runs appear in the Actions tab
- Each run shows timestamp and status (✅ success or ❌ failed)

## 📧 What Happens Next

### When Discount is Below Target (2%):
- Workflow runs every 30 minutes
- Logs show: "Target not reached yet. Waiting..."
- No email is sent

### When Discount Reaches 2% or Higher:
- 🎉 You receive a beautiful HTML email alert
- Email subject: "🎉 Flipkart Voucher Alert: X% OFF Available!"
- Email includes direct link to purchase
- Workflow continues monitoring every 30 minutes

## 🔍 Monitoring & Troubleshooting

### View Logs:
1. Go to **Actions** tab
2. Click any workflow run
3. Click **"check-discount"** job
4. Expand **"Check Flipkart discount"** to see full output

### Common Issues:

**No workflow runs appearing?**
- Wait up to 5 minutes after enabling Actions
- Check if Actions is enabled in repository Settings → Actions → General
- Make sure the workflow file is in `.github/workflows/` directory

**"Authentication failed" error?**
- Double-check you're using App Password, not regular password
- Verify all 3 secrets are set correctly (no extra spaces)
- Secret names must be EXACTLY: `SENDER_EMAIL`, `RECEIVER_EMAIL`, `EMAIL_PASSWORD`

**Not receiving emails?**
- Check spam/junk folder
- Verify RECEIVER_EMAIL is correct
- Ensure 2-Step Verification is enabled on Gmail
- Regenerate App Password if needed

**"Could not extract discount" in logs?**
- The website structure may have changed
- The script will keep trying every 30 minutes
- Check if the URL is still accessible

## ⚙️ Customization

### Change Target Discount:
Edit `check_discount.py` line 26:
```python
TARGET_DISCOUNT = 2.0  # Change to desired percentage
```

### Change Schedule:
Edit `.github/workflows/discount-checker.yml` line 6:
```yaml
- cron: '*/30 * * * *'  # Current: every 30 minutes
```

Examples:
- `0 * * * *` - Every hour
- `0 */2 * * *` - Every 2 hours
- `0 9-21 * * *` - Every hour from 9 AM to 9 PM
- `*/15 * * * *` - Every 15 minutes

After making changes, commit and push to GitHub.

## 💰 Cost

**Completely FREE!** GitHub Actions provides:
- 2,000 free minutes/month (private repos)
- Unlimited minutes (public repos)

This workflow uses ~1-2 minutes per day = well within free tier.

## 🔒 Security Tips

- ✅ Use App Passwords (never regular Gmail password)
- ✅ Store credentials in GitHub Secrets (never in code)
- ✅ Consider making repository private for security
- ✅ Regularly rotate App Passwords every few months

## 📊 Expected Behavior

Once set up:
- ✅ Runs automatically every 30 minutes, 24/7
- ✅ No server or computer needed
- ✅ Works even when your computer is off
- ✅ Free GitHub infrastructure handles everything
- ✅ Email alerts arrive within seconds of target discount

---

**Your automation is ready! Complete the 4 steps above and you'll be monitoring discounts automatically! 🚀**

