#!/bin/bash

# Controlvar Marketing Website Deployment Script
# Usage: ./deploy.sh [environment]
# Environments: dev, staging, prod

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration
ENVIRONMENT=${1:-prod}
BUCKET_NAME="controlvar.com"
CLOUDFRONT_DISTRIBUTION_ID=""  # Set this after creating CloudFront distribution

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}   Controlvar Marketing Deployment${NC}"
echo -e "${BLUE}   Environment: ${ENVIRONMENT}${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# Check if AWS CLI is installed
if ! command -v aws &> /dev/null; then
    echo -e "${RED}❌ AWS CLI not found. Please install it first.${NC}"
    exit 1
fi

# Check if Next.js config allows static export
echo -e "\n${BLUE}📋 Checking Next.js configuration...${NC}"
if ! grep -q '"output".*"export"' next.config.js 2>/dev/null; then
    echo -e "${RED}⚠️  Next.js is not configured for static export${NC}"
    echo -e "Add this to next.config.js:"
    echo -e "  output: 'export',"
    echo -e "  images: { unoptimized: true },"
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Install dependencies
echo -e "\n${BLUE}📦 Installing dependencies...${NC}"
npm ci --production=false

# Build the site
echo -e "\n${BLUE}🔨 Building Next.js application...${NC}"
# The MARKETING build. A plain `npm run build` ships the whole dashboard --
# /experiments, /admin/*, /feature-flags, /results, /workspaces and three
# "Sign in" buttons -- to a site with no API behind it, where every one of
# them 404s. See frontend/src/utils/site-mode.ts.
npm run build:marketing

# Check if build was successful
if [ ! -d "out" ]; then
    echo -e "${RED}❌ Build failed - 'out' directory not found${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Build successful${NC}"

# Upload to S3
echo -e "\n${BLUE}☁️  Uploading to S3...${NC}"

# Sync static assets with long cache
echo -e "  Uploading static assets (1 year cache)..."
aws s3 sync out/ s3://${BUCKET_NAME} \
  --delete \
  --cache-control "public, max-age=31536000, immutable" \
  --exclude "*.html" \
  --exclude "*.json" \
  --exclude "*.txt" \
  --quiet

# Sync HTML and JSON with short cache
echo -e "  Uploading HTML/JSON files (no cache)..."
aws s3 sync out/ s3://${BUCKET_NAME} \
  --delete \
  --cache-control "public, max-age=0, must-revalidate" \
  --exclude "*" \
  --include "*.html" \
  --include "*.json" \
  --include "*.txt" \
  --quiet

echo -e "${GREEN}✅ Upload complete${NC}"

# Invalidate CloudFront cache.
#
# This used to DISCOVER the distribution, PRINT it, tell you to paste it back
# into the script, and then not invalidate. CLOUDFRONT_DISTRIBUTION_ID was
# empty, so nothing ever ran: the objects carry s-maxage=31536000 and
# CloudFront served the previous deploy for up to a YEAR. That is not
# hypothetical -- /power-calculator served the homepage's HTML long after a
# build had fixed it.
#
# So: use the id it finds, and FAIL if it cannot find one. A deploy that
# uploads new files and leaves the old ones being served has not deployed
# anything, and saying so loudly beats a green tick.
if [ -z "$CLOUDFRONT_DISTRIBUTION_ID" ]; then
    echo -e "\n${BLUE}🔎 Finding the CloudFront distribution for ${BUCKET_NAME}...${NC}"
    CLOUDFRONT_DISTRIBUTION_ID=$(aws cloudfront list-distributions \
      --query "DistributionList.Items[?Aliases.Items[?contains(@, '${BUCKET_NAME}')]].Id | [0]" \
      --output text 2>/dev/null || echo "")
    [ "$CLOUDFRONT_DISTRIBUTION_ID" = "None" ] && CLOUDFRONT_DISTRIBUTION_ID=""
fi

if [ -z "$CLOUDFRONT_DISTRIBUTION_ID" ]; then
    echo -e "\n${RED}❌ No CloudFront distribution found for ${BUCKET_NAME}.${NC}"
    echo -e "   The files are in S3 but CloudFront will keep serving the old ones."
    echo -e "   Set CLOUDFRONT_DISTRIBUTION_ID explicitly, or check the IAM user can"
    echo -e "   call cloudfront:ListDistributions."
    exit 1
fi

echo -e "\n${BLUE}🔄 Invalidating CloudFront cache (${CLOUDFRONT_DISTRIBUTION_ID})...${NC}"
INVALIDATION_ID=$(aws cloudfront create-invalidation \
  --distribution-id "${CLOUDFRONT_DISTRIBUTION_ID}" \
  --paths "/*" \
  --query 'Invalidation.Id' \
  --output text)
echo -e "  Invalidation ID: ${INVALIDATION_ID}"
echo -e "${GREEN}✅ Cache invalidation started${NC}"
echo -e "   It is not finished. Until it completes, CloudFront may still serve"
echo -e "   the previous deploy. Watch it with:"
echo -e "     aws cloudfront wait invalidation-completed \\"
echo -e "       --distribution-id ${CLOUDFRONT_DISTRIBUTION_ID} --id ${INVALIDATION_ID}"

# Display deployment info
echo -e "\n${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ Deployment Complete!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "\n📊 Deployment Summary:"
echo -e "  Environment: ${ENVIRONMENT}"
echo -e "  S3 Bucket: s3://${BUCKET_NAME}"
echo -e "  Website: https://${BUCKET_NAME}"
echo -e "\n🌍 URLs:"
echo -e "  Marketing: ${GREEN}https://controlvar.com${NC}"
echo -e "  App:       https://app.controlvar.com"
echo -e "  API:       https://api.controlvar.com"
echo -e "  Docs:      https://docs.controlvar.com"

# Test deployment
echo -e "\n${BLUE}🧪 Testing deployment...${NC}"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" https://${BUCKET_NAME} || echo "000")

if [ "$HTTP_CODE" == "200" ]; then
    echo -e "${GREEN}✅ Website is live and responding${NC}"
elif [ "$HTTP_CODE" == "000" ]; then
    echo -e "${RED}⚠️  Could not connect to website${NC}"
    echo -e "   DNS may still be propagating (wait 5-10 minutes)"
else
    echo -e "${RED}⚠️  Website returned HTTP ${HTTP_CODE}${NC}"
fi

echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "Next steps:"
echo -e "  1. Visit https://controlvar.com to verify"
echo -e "  2. Test on mobile devices"
echo -e "  3. Check Google PageSpeed Insights"
echo -e "  4. Set up monitoring and analytics"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
