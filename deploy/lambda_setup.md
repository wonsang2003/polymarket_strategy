# AWS Lambda Deployment Guide

## 1. Create Telegram Bot

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`, follow the prompts, pick a name
3. Copy the **bot token** (looks like `123456789:ABCdefGHI...`)
4. Message your new bot (send anything like "hi")
5. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
6. Find `"chat":{"id": 123456789}` — that number is your **chat ID**

## 2. Create S3 Bucket (optional, for state persistence)

```bash
aws s3 mb s3://polymarket-whale-monitor --region us-east-1
```

Without S3, the monitor rediscovers ELITE whales on every invocation (slower but works).

## 3. Package the Lambda

```bash
bash deploy/package_lambda.sh
```

This creates `deploy/lambda.zip`.

## 4. Create Lambda Function

```bash
aws lambda create-function \
  --function-name polymarket-whale-monitor \
  --runtime python3.12 \
  --handler lambda_handler.handler \
  --zip-file fileb://deploy/lambda.zip \
  --role arn:aws:iam::YOUR_ACCOUNT:role/lambda-basic-role \
  --timeout 300 \
  --memory-size 256 \
  --environment Variables="{
    TELEGRAM_BOT_TOKEN=your_bot_token,
    TELEGRAM_CHAT_ID=your_chat_id,
    WHALE_MIN_SIZE=1000,
    S3_STATE_BUCKET=polymarket-whale-monitor,
    S3_STATE_KEY=whale_monitor_state.json
  }"
```

Note: The Lambda role needs `s3:GetObject` and `s3:PutObject` permissions on the state bucket if using S3.

## 5. Create EventBridge Schedule

```bash
# Create the rule (every hour)
aws events put-rule \
  --name polymarket-whale-monitor-hourly \
  --schedule-expression "rate(1 hour)"

# Add Lambda as target
aws events put-targets \
  --rule polymarket-whale-monitor-hourly \
  --targets "Id"="1","Arn"="arn:aws:lambda:us-east-1:YOUR_ACCOUNT:function:polymarket-whale-monitor"

# Grant EventBridge permission to invoke the Lambda
aws lambda add-permission \
  --function-name polymarket-whale-monitor \
  --statement-id eventbridge-hourly \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:us-east-1:YOUR_ACCOUNT:rule/polymarket-whale-monitor-hourly
```

## 6. Test

```bash
# Invoke manually
aws lambda invoke --function-name polymarket-whale-monitor output.json
cat output.json
```

## 7. Update

```bash
bash deploy/package_lambda.sh
aws lambda update-function-code \
  --function-name polymarket-whale-monitor \
  --zip-file fileb://deploy/lambda.zip
```

## Cost Estimate

- 720 invocations/month (hourly)
- ~5 seconds per invocation at 256MB
- **~$0.01/month** (well within free tier)
