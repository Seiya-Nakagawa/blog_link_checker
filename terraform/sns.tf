resource "aws_sns_topic" "sns_topic_system" {
  name         = "${var.system_name}-${var.env}-sns-system"
  display_name = "${var.system_name}-${var.env}-sns-system"

  delivery_policy = jsonencode({
    "http" : {
      "defaultHealthyRetryPolicy" : {
        "minDelayTarget"     : 20,
        "maxDelayTarget"     : 20,
        "numRetries"         : 3,
        "numMaxDelayRetries" : 0,
        "numNoDelayRetries"  : 0,
        "numMinDelayRetries" : 0,
        "backoffFunction"    : "linear"
      },
      "disableSubscriptionOverrides" : false,
      "defaultThrottlePolicy" : {
        "maxReceivesPerSecond" : 1
      }
    }
  })
}

resource "aws_sns_topic_policy" "sns_topic_policy_system" {
  arn    = aws_sns_topic.sns_topic_system.arn
  policy = data.aws_iam_policy_document.sns_topic_policy_document_system.json
}

resource "aws_sns_topic_subscription" "email_target" {
  for_each = toset(distinct(concat(var.notification_emails_blog, var.notification_emails)))

  topic_arn = aws_sns_topic.sns_topic_system.arn
  protocol  = "email"
  endpoint  = each.value
}
