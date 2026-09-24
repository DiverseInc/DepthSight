// src/components/onboarding/OnboardingWizard.tsx

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  CheckCircle2,
  FlaskConical,
  KeyRound,
  Rocket,
  Sparkles,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/components/ui/use-toast";

interface OnboardingWizardProps {
  /** When true, wizard is rendered as a full-screen page; otherwise as an embedded card */
  embedded?: boolean;
  /** Called after paper strategy is started (so the parent can dismiss the wizard) */
  onComplete?: () => void;
}

type StepIndex = 1 | 2 | 3 | 4;

// ----- API call helpers --------------------------------------------------------

async function startPaperStrategy(payload: {
  template_slug: string;
  symbol: string;
  timeframe: string;
}) {
  const res = await fetch("/api/v1/onboarding/start-paper-strategy", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${localStorage.getItem("authToken") || ""}`,
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  const json = await res.json();
  return json.data as {
    config_id: string;
    strategy_instance_id: string;
    template_slug: string;
    symbol: string;
    timeframe: string;
    status: string;
  };
}

async function validateOkxKey(payload: {
  api_key: string;
  api_secret: string;
  passphrase: string;
}) {
  const res = await fetch("/api/v1/onboarding/validate-okx-key", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${localStorage.getItem("authToken") || ""}`,
    },
    body: JSON.stringify(payload),
  });
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  const json = await res.json();
  return json.data as {
    valid: boolean;
    error: string | null;
    account_type: string | null;
  };
}

async function convertToLive(payload: { config_id: string }) {
  const res = await fetch("/api/v1/onboarding/convert-to-live", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${localStorage.getItem("authToken") || ""}`,
    },
    body: JSON.stringify(payload),
  });
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  const json = await res.json();
  return json.data as {
    config_id: string;
    run_mode: string;
    api_key_id: number | null;
    status: string;
  };
}

// ----- The component ----------------------------------------------------------

export const OnboardingWizard = ({
  embedded = false,
  onComplete,
}: OnboardingWizardProps) => {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [step, setStep] = useState<StepIndex>(1);
  const [configId, setConfigId] = useState<string | null>(null);
  const [okxKey, setOkxKey] = useState({ api_key: "", api_secret: "", passphrase: "" });
  const [okxValid, setOkxValid] = useState<boolean | null>(null);

  // ---- Mutations -------------------------------------------------------------
  const startPaper = useMutation({
    mutationFn: startPaperStrategy,
    onSuccess: (data) => {
      setConfigId(data.config_id);
      setStep(2);
      toast({
        title: "Paper strategy started",
        description: `RSI Breakout v2 on ${data.symbol} ${data.timeframe} is running on paper.`,
      });
      qc.invalidateQueries({ queryKey: ["strategies"] });
      qc.invalidateQueries({ queryKey: ["candleHealth"] });
    },
    onError: (err: Error) => {
      toast({
        title: "Could not start paper strategy",
        description: err.message,
        variant: "destructive",
      });
    },
  });

  const validateOkx = useMutation({
    mutationFn: validateOkxKey,
    onSuccess: (data) => {
      if (data.valid) {
        setOkxValid(true);
        setStep(3);
        toast({
          title: "OKX key validated",
          description:
            data.account_type
              ? `Live trading on OKX (${data.account_type}) will be enabled.`
              : "Live trading on OKX will be enabled.",
        });
      } else {
        setOkxValid(false);
        toast({
          title: "OKX key validation failed",
          description: data.error ?? "Check key / secret / passphrase.",
          variant: "destructive",
        });
      }
    },
    onError: (err: Error) => {
      toast({
        title: "Could not reach OKX",
        description: err.message,
        variant: "destructive",
      });
    },
  });

  const convertLive = useMutation({
    mutationFn: convertToLive,
    onSuccess: () => {
      toast({
        title: "Strategy converted to live",
        description: "Your strategy is now trading real orders on OKX.",
      });
      setStep(4);
      qc.invalidateQueries({ queryKey: ["strategies"] });
    },
    onError: (err: Error) => {
      toast({
        title: "Could not convert to live",
        description: err.message,
        variant: "destructive",
      });
    },
  });

  // ---- Render ---------------------------------------------------------------

  const skipPaper = () => {
    setStep(2);
    toast({
      title: "Skipped paper trading",
      description: "You can pick one later from Strategies → + New.",
    });
  };

  const skipOkx = () => {
    setStep(3);
    toast({
      title: "Skipped OKX key",
      description: "Add one later from Settings → API Keys.",
    });
  };

  const finish = () => {
    onComplete?.();
    navigate("/");
  };

  const goDashboard = () => {
    onComplete?.();
    navigate("/dashboard");
  };

  const containerClass = embedded
    ? "w-full max-w-2xl mx-auto"
    : "w-full max-w-2xl mx-auto py-12";

  return (
    <div className={containerClass}>
      <Card>
        <CardContent className="p-6 sm:p-8">
          {/* Progress bar */}
          <div className="mb-6 flex items-center gap-2">
            {[1, 2, 3].map((s) => (
              <div
                key={s}
                className={
                  "h-1.5 flex-1 rounded-full transition-colors " +
                  (s <= step ? "bg-primary" : "bg-muted")
                }
              />
            ))}
          </div>

          {/* Step 1: Welcome + Start paper */}
          {step === 1 && (
            <Step1
              loading={startPaper.isPending}
              onStart={() =>
                startPaper.mutate({
                  template_slug: "rsi-breakout-v2",
                  symbol: "BTCUSDT",
                  timeframe: "1h",
                })
              }
              onSkip={skipPaper}
            />
          )}

          {/* Step 2: OKX key */}
          {step === 2 && (
            <Step2
              okxKey={okxKey}
              setOkxKey={setOkxKey}
              valid={okxValid}
              loading={validateOkx.isPending}
              onValidate={() => validateOkx.mutate(okxKey)}
              onSkip={skipOkx}
              onBack={() => setStep(1)}
            />
          )}

          {/* Step 3: Convert to live */}
          {step === 3 && (
            <Step3
              configId={configId}
              loading={convertLive.isPending}
              onConvert={() => configId && convertLive.mutate({ config_id: configId })}
              onBack={() => setStep(2)}
              onFinish={goDashboard}
              onSkip={finish}
            />
          )}

          {/* Step 4: Done */}
          {step === 4 && (
            <Step4 onFinish={goDashboard} />
          )}
        </CardContent>
      </Card>
    </div>
  );
};

// ----- Steps -----------------------------------------------------------------

const StepHeader = ({
  icon: Icon,
  title,
  body,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  body: string;
}) => (
  <div className="mb-5">
    <div className="mb-3 inline-flex h-12 w-12 items-center justify-center rounded-xl bg-primary/10 text-primary">
      <Icon className="h-6 w-6" />
    </div>
    <h2 className="text-xl font-semibold">{title}</h2>
    <p className="mt-1 text-sm text-muted-foreground">{body}</p>
  </div>
);

const Step1 = ({
  loading,
  onStart,
  onSkip,
}: {
  loading: boolean;
  onStart: () => void;
  onSkip: () => void;
}) => (
  <div>
    <StepHeader
      icon={FlaskConical}
      title="Start a paper strategy in one click"
      body="We'll create RSI Breakout v2 on BTCUSDT 1h and run it on paper (simulated fills against real OKX candles, no money at risk)."
    />
    <ul className="mb-6 space-y-2 text-sm text-muted-foreground">
      <li>• $10,000 simulated USDT starting balance</li>
      <li>• Default 5% position size · 2% stop loss · 4% take profit</li>
      <li>• Cancel any time from the dashboard</li>
    </ul>
    <div className="flex items-center gap-3">
      <Button onClick={onStart} disabled={loading} size="lg">
        {loading ? "Starting…" : "Start RSI Breakout v2"}
        <ArrowRight className="ml-2 h-4 w-4" />
      </Button>
      <Button variant="ghost" onClick={onSkip} disabled={loading}>
        I'll pick later
      </Button>
    </div>
  </div>
);

const Step2 = ({
  okxKey,
  setOkxKey,
  valid,
  loading,
  onValidate,
  onSkip,
  onBack,
}: {
  okxKey: { api_key: string; api_secret: string; passphrase: string };
  setOkxKey: (v: { api_key: string; api_secret: string; passphrase: string }) => void;
  valid: boolean | null;
  loading: boolean;
  onValidate: () => void;
  onSkip: () => void;
  onBack: () => void;
}) => (
  <div>
    <StepHeader
      icon={KeyRound}
      title="Connect your OKX key (optional)"
      body="Paste your OKX API key to enable live trading. We validate by reading your balance — never enable Withdraw on the OKX side."
    />
    <div className="space-y-3">
      <div>
        <Label htmlFor="okx-key">API Key</Label>
        <Input
          id="okx-key"
          value={okxKey.api_key}
          onChange={(e) => setOkxKey({ ...okxKey, api_key: e.target.value })}
          placeholder="Paste OKX API key"
          autoComplete="off"
          spellCheck={false}
        />
      </div>
      <div>
        <Label htmlFor="okx-secret">Secret</Label>
        <Input
          id="okx-secret"
          type="password"
          value={okxKey.api_secret}
          onChange={(e) => setOkxKey({ ...okxKey, api_secret: e.target.value })}
          placeholder="Paste OKX API secret"
          autoComplete="off"
          spellCheck={false}
        />
      </div>
      <div>
        <Label htmlFor="okx-passphrase">Passphrase</Label>
        <Input
          id="okx-passphrase"
          type="password"
          value={okxKey.passphrase}
          onChange={(e) => setOkxKey({ ...okxKey, passphrase: e.target.value })}
          placeholder="Paste OKX passphrase"
          autoComplete="off"
          spellCheck={false}
        />
      </div>
    </div>
    {valid === false && (
      <p className="mt-3 text-sm text-destructive">
        Validation failed. Check key/secret/passphrase and try again.
      </p>
    )}
    <div className="mt-6 flex items-center gap-3">
      <Button onClick={onValidate} disabled={loading || !okxKey.api_key || !okxKey.api_secret || !okxKey.passphrase} size="lg">
        {loading ? "Validating…" : "Validate key"}
      </Button>
      <Button variant="ghost" onClick={onSkip} disabled={loading}>
        Skip — I'll add later
      </Button>
      <Button variant="ghost" onClick={onBack} disabled={loading}>
        Back
      </Button>
    </div>
  </div>
);

const Step3 = ({
  configId,
  loading,
  onConvert,
  onBack,
  onFinish,
  onSkip,
}: {
  configId: string | null;
  loading: boolean;
  onConvert: () => void;
  onBack: () => void;
  onFinish: () => void;
  onSkip: () => void;
}) => (
  <div>
    <StepHeader
      icon={Rocket}
      title="Ready to go live?"
      body="Swap your paper strategy to live with one click. You can roll back to paper at any time."
    />
    <p className="mb-6 text-sm text-muted-foreground">
      Live trading uses real money. We'll attach your most-recent active OKX key and update your plan's quota accordingly.
    </p>
    <div className="flex items-center gap-3">
      <Button onClick={onConvert} disabled={loading || !configId} size="lg">
        {loading ? "Converting…" : "Go live"}
        <ArrowRight className="ml-2 h-4 w-4" />
      </Button>
      <Button variant="ghost" onClick={onFinish} disabled={loading}>
        Stay on paper — finish
      </Button>
      <Button variant="ghost" onClick={onBack} disabled={loading}>
        Back
      </Button>
    </div>
  </div>
);

const Step4 = ({ onFinish }: { onFinish: () => void }) => (
  <div className="text-center">
    <div className="mx-auto mb-4 inline-flex h-14 w-14 items-center justify-center rounded-2xl bg-profit/10 text-profit">
      <CheckCircle2 className="h-8 w-8" />
    </div>
    <h2 className="text-xl font-semibold">You're set up</h2>
    <p className="mt-2 text-sm text-muted-foreground">
      Your strategy is running. The dashboard will show candle flow + P&L within minutes.
    </p>
    <div className="mt-6 flex items-center justify-center gap-3">
      <Button onClick={onFinish} size="lg">
        Open dashboard
        <ArrowRight className="ml-2 h-4 w-4" />
      </Button>
    </div>
  </div>
);
