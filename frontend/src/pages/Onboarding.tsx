// src/pages/Onboarding.tsx
//
// 5-minute onboarding wizard. Mounted at /onboarding.
// Auto-runs for brand-new users (AuthContext sets `needs_onboarding` after signup).

import { ArrowLeft, Sparkles } from "lucide-react";
import { Link, Navigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { MarketingNav } from "@/components/marketing/MarketingNav";
import { OnboardingWizard } from "@/components/onboarding/OnboardingWizard";
import { Button } from "@/components/ui/button";

const Onboarding = () => {
  const { token, user, isLoading } = useAuth();

  if (isLoading) return null;
  if (!token || !user) {
    return <Navigate to="/login" replace />;
  }

  return (
    <div className="min-h-screen bg-background">
      <MarketingNav />
      <main className="container max-w-3xl px-4 py-8">
        <div className="mb-6 text-center">
          <div className="mb-3 inline-flex items-center gap-2 rounded-full border border-border/60 bg-background/50 px-3 py-1 text-xs font-medium text-muted-foreground">
            <Sparkles className="h-3.5 w-3.5 text-primary" />
            <span>From signup to first fill in 5 minutes</span>
          </div>
          <h1 className="text-2xl font-bold tracking-tight sm:text-3xl">
            Welcome to DepthSight
          </h1>
          <p className="mt-2 text-sm text-muted-foreground">
            Three quick steps — skip any of them, you can do everything later.
          </p>
        </div>

        <OnboardingWizard />

        <div className="mt-8 text-center">
          <Button asChild variant="ghost" size="sm">
            <Link to="/">
              <ArrowLeft className="mr-2 h-4 w-4" />
              Skip to dashboard
            </Link>
          </Button>
        </div>
      </main>
    </div>
  );
};

export default Onboarding;
