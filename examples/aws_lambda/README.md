# Example AWS Lambda function for Honeybadger app

This is an example AWS Lambda function.

# Pre-requisites

Install AWS-CLI
https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2-mac.html#cliv2-mac-prereq

## Deploying

Install honeybadger in a package directory with pip's --target option
`pip install --target ./package honeybadger`

`Navigate to package directory
`cd package`

Create a deployment package with the installed libraries at the root.
`zip -r ../test-honeybadger-lambda.zip .`

Navigate back to the my-function directory.
`cd ..`

Add function code files to the root of your deployment package.
`zip -g test-honeybadger-lambda.zip handler.py`

use aws-cli update function code command to upload the binary .zip file to Lambda and update the function code.
```aws_lambda % aws lambda create-function\
    --function-name HoneyBadgerLambda \
    --zip-file fileb://test-honeybadger-lambda.zip\
    --role *your role arn here*\
    --region us-east-1 --runtime python3.6 --handler handler.lambda_handler
    ```
