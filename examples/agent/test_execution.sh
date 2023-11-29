#!/usr/bin/env bash
# Very simple script to retrieve the AWS CF Apollo Agent registration information and locally test invocation with the
# generated role via the health endpoint (dev). Requires setting a profile, region, and specifying a standard agent stack.

region='us-east-1'
profile_name='<PROFILE'
stack_arn='<STACK_ARN>'

stack_outputs=$(aws cloudformation describe-stacks --stack-name $stack_arn --profile $profile_name --query 'Stacks[].Outputs[*]')
invocation_role=$(jq -r '.[][] | select(.OutputKey=="InvocationRoleArn") | .OutputValue' <<<"$stack_outputs")
invocation_external_id=$(jq -r '.[][] | select(.OutputKey=="InvocationRoleExternalId") | .OutputValue' <<<"$stack_outputs")
function_arn=$(jq -r '.[][] | select(.OutputKey=="FunctionArn") | .OutputValue' <<<"$stack_outputs")

temp_role=$(aws sts assume-role --profile $profile_name --role-arn "$invocation_role" --external-id "$invocation_external_id" --role-session-name 'mc-agent-test')

AWS_ACCESS_KEY_ID=$(jq -r .Credentials.AccessKeyId <<<"$temp_role") \
AWS_SECRET_ACCESS_KEY=$(jq -r .Credentials.SecretAccessKey <<<"$temp_role") \
AWS_SESSION_TOKEN=$(jq -r .Credentials.SessionToken <<<"$temp_role") \
AWS_REGION=$region \
  aws lambda invoke \
  --function-name "$function_arn" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"path": "/api/v1/test/health", "httpMethod": "GET", "queryStringParameters": {"trace_id": "123456789", "full": true}}' \
  /dev/stdout | jq '.body | select( . != null ) | fromjson'
