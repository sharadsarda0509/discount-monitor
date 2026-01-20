# Flipkart Discount Monitor 🛒

Automated discount monitoring for Flipkart E-Gift Vouchers using GitHub Actions. Get email alerts when your target discount is available!

## 🎯 Features

- ✅ Runs automatically every 30 minutes via GitHub Actions
- ✅ Monitors Flipkart voucher discount percentage
- ✅ Sends beautiful HTML email alerts when target discount (2%) is reached
- ✅ No server needed - runs entirely on GitHub's free infrastructure
- ✅ Easy to configure and deploy

## 📋 Prerequisites

- A GitHub account
- A Gmail account (for sending email alerts)

## 🚀 Setup Instructions

### Step 1: Fork or Create Repository

1. Create a new **private** repository on GitHub (recommended for privacy)
2. Clone this folder's contents to your new repository

### Step 2: Configure Gmail App Password

Since you'll be using Gmail to send alerts, you need to create an App Password:

1. Go to your Google Account: https://myaccount.google.com/
2. Navigate to **Security**
3. Enable **2-Step Verification** (if not already enabled)
4. Go to **App passwords**: https://myaccount.google.com/apppasswords
5. Select **Mail** and **Other (Custom name)** → Enter "Flipkart Monitor"
6. Click **Generate**
7. Copy the 16-character password (you'll use this in Step 3)

### Step 3: Add GitHub Secrets

Go to your repository on GitHub:
1. Click **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret** and add these three secrets:

| Secret Name | Value | Description |
|-------------|-------|-------------|
| `SENDER_EMAIL` | your-email@gmail.com | Your Gmail address |
| `RECEIVER_EMAIL` | your-email@gmail.com | Email where you want to receive alerts (can be same) |
| `EMAIL_PASSWORD` | *app-password* | The 16-character app password from Step 2 |

### Step 4: Enable GitHub Actions

1. Go to your repository's **Actions** tab
2. If prompted, click **"I understand my workflows, go ahead and enable them"**
3. The workflow will now run automatically every 30 minutes

### Step 5: Test the Setup (Optional)

To test without waiting 30 minutes:

1. Go to **Actions** tab
2. Click on **"Flipkart Discount Monitor"** workflow
3. Click **"Run workflow"** → **"Run workflow"** button
4. Wait a few seconds and check the run results

## 📧 Email Alert Preview

When the target discount is reached, you'll receive an email like this:

```
Subject: 🎉 Flipkart Voucher Alert: 2% OFF Available!

The Flipkart E-Gift Voucher INR 10000 has reached 2% OFF!

Current Price: ₹9800

[Buy Now on OnePlay Store]

⏰ Alert triggered at: 2026-01-20 15:30:00 IST
```

## ⚙️ Configuration

### Customize Target Discount

Edit `check_discount.py` and change this line:

```python
TARGET_DISCOUNT = 2.0  # Change to your desired percentage
```

### Customize Monitoring URL

Edit `check_discount.py` and change this line:

```python
URL = "https://store.oneplay.in/view/flipkart-e-gift-voucher-inr-10000-..."
```

### Change Schedule

Edit `.github/workflows/discount-checker.yml` and modify the cron expression:

```yaml
schedule:
  - cron: '*/30 * * * *'  # Current: Every 30 minutes
  # Examples:
  # - cron: '0 * * * *'    # Every hour
  # - cron: '0 */2 * * *'  # Every 2 hours
  # - cron: '0 9-21 * * *' # Every hour between 9 AM - 9 PM
```

**Cron Expression Guide:**
- `*/30 * * * *` - Every 30 minutes
- `0 * * * *` - Every hour at minute 0
- `0 */2 * * *` - Every 2 hours
- `0 9-21 * * *` - Every hour from 9 AM to 9 PM

## 🔍 Monitoring & Logs

### View Logs

1. Go to **Actions** tab in your repository
2. Click on any workflow run
3. Click on **"check-discount"** job
4. Expand the **"Check Flipkart discount"** step to see logs

Example log output:
```
============================================================
Flipkart Discount Monitor - Run at 2026-01-20 15:30:00
============================================================
[2026-01-20 15:30:00] Fetching URL: https://store.oneplay.in/...
[2026-01-20 15:30:02] Successfully fetched page (Status: 200)
[2026-01-20 15:30:02] Found discount: 1%
[2026-01-20 15:30:02] Current discount: 1% | Target: 2%
[2026-01-20 15:30:02] Target not reached yet. Waiting...
```

## 📂 File Structure

```
flipkart-discount-monitor/
├── .github/
│   └── workflows/
│       └── discount-checker.yml    # GitHub Actions workflow
├── check_discount.py               # Main Python script
├── requirements.txt                # Python dependencies
├── .gitignore                      # Git ignore file
└── README.md                       # This file
```

## 🐛 Troubleshooting

### Workflow not running?

- Check if GitHub Actions is enabled in repository settings
- Verify the workflow file is in `.github/workflows/` directory
- GitHub Actions might have a delay of a few minutes

### Not receiving emails?

- Verify all three secrets are set correctly in GitHub
- Check your Gmail spam/junk folder
- Ensure 2-Step Verification and App Password are set up correctly
- Check workflow logs for error messages

### "Authentication failed" error?

- Make sure you're using an **App Password**, not your regular Gmail password
- Regenerate the App Password if needed

### Script not detecting discount?

- The website structure may have changed
- Check the logs to see what discount was detected
- You may need to update the scraping logic in `check_discount.py`

## 💰 Cost

**100% FREE!** 

GitHub Actions provides 2,000 free minutes per month for private repositories and unlimited for public repositories. This workflow uses about 1-2 minutes per day, well within the free tier.

## 🔒 Security

- Always use **App Passwords**, never your actual Gmail password
- Store sensitive data in GitHub Secrets (never commit them to code)
- Consider using a private repository if you don't want others to see your monitoring setup

## 📝 License

This project is open source and available for personal use.

## 🤝 Contributing

Feel free to customize this for your own needs! Some ideas:
- Monitor multiple vouchers
- Add Telegram/Slack notifications
- Create a dashboard with historical pricing
- Add SMS alerts

## 📞 Support

If you encounter issues:
1. Check the **Troubleshooting** section above
2. Review workflow logs in GitHub Actions
3. Ensure all secrets are configured correctly

---

**Happy Shopping! 🛍️**

*Last Updated: January 2026*

