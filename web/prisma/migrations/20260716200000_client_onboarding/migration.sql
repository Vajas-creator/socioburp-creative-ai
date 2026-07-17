-- CreateTable
CREATE TABLE "web_client_onboarding" (
    "id" UUID NOT NULL,
    "user_id" UUID NOT NULL,
    "business_name" TEXT,
    "industry" TEXT,
    "business_description" TEXT,
    "website" TEXT,
    "business_address" TEXT,
    "time_zone" TEXT,
    "owner_name" TEXT,
    "contact_email" TEXT,
    "phone_number" TEXT,
    "whatsapp_number" TEXT,
    "marketing_goals" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "custom_goal" TEXT,
    "facebook_url" TEXT,
    "instagram_url" TEXT,
    "linkedin_url" TEXT,
    "youtube_url" TEXT,
    "google_business_url" TEXT,
    "twitter_url" TEXT,
    "google_ads_account_id" TEXT,
    "meta_ads_account_id" TEXT,
    "monthly_budget" INTEGER,
    "target_locations" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "last_completed_step" INTEGER NOT NULL DEFAULT 0,
    "completed_at" TIMESTAMP(3),
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "web_client_onboarding_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "web_client_onboarding_user_id_key" ON "web_client_onboarding"("user_id");

-- AddForeignKey
ALTER TABLE "web_client_onboarding" ADD CONSTRAINT "web_client_onboarding_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth_users"("id") ON DELETE CASCADE ON UPDATE CASCADE;

