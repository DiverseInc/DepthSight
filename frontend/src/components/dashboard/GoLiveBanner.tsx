// src/components/dashboard/GoLiveBanner.tsx
// Shown above the dashboard when a user has no active exchange API keys.
// Surfaces the OKX referral link so any new-to-crypto user signs up via
// our link before adding a key. Trades through the referral earn us a fee
// kickback that funds further development.

import { ArrowRight, KeyRound } from "lucide-react";
import { Link } from "react-router-dom";
import { useActiveApiKeys } from "@/lib/api";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";

const OKX_REFERRAL_URL = "https://app.okx.com/en-us/join/30180071";

export const GoLiveBanner = () => {
  const activeKeys = useActiveApiKeys();

  // Hide once the user has at least one active API key — they don't need
  // the referral anymore. Auto re-appears if the key gets disabled/deleted.
  if (activeKeys.length > 0) return null;

  return (
    <Card className="border-amber-500/30 bg-gradient-to-br from-amber-500/5 via-card/40 to-card/40">
      <CardHeader className="pb-3">
        <div className="flex items-start gap-3">
          <div className="shrink-0 inline-flex items-center justify-center w-10 h-10 rounded-lg bg-amber-500/10 text-amber-500">
            <KeyRound className="w-5 h-5" />
          </div>
          <div className="min-w-0 flex-1 space-y-1">
            <CardTitle className="text-base md:text-lg">
              Ready to go live? You'll need an OKX account.
            </CardTitle>
            <CardDescription>
              Don't have one yet?{" "}
              <a
                href={OKX_REFERRAL_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="text-amber-500 underline underline-offset-2 hover:text-amber-400"
              >
                Sign up with our referral link
              </a>{" "}
              — Trade-fee kickback helps us build more features. After signup,
              create an API key with <strong>Trade</strong> permission only
              (never Withdraw) at{" "}
              <a
                href="https://www.okx.com/account/my-account/api"
                target="_blank"
                rel="noopener noreferrer"
                className="text-amber-500 underline underline-offset-2"
              >
                okx.com → Account → API
              </a>
              , then paste it below.
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="pt-0">
        <Button asChild variant="outline" size="sm">
          <Link to="/settings">
            Add API key
            <ArrowRight className="w-3 h-3 ml-1 inline-block" />
          </Link>
        </Button>
      </CardContent>
    </Card>
  );
};

export default GoLiveBanner;