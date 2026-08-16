/**
 * Strategy Style Wizard
 * ─────────────────────
 * 4-question wizard that recommends 3-5 strategies from the curated
 * library based on the user's answers.
 *
 *   Q1. Time horizon      → Scalping / Day / Swing / Position
 *   Q2. Risk tolerance    → Low / Medium / High
 *   Q3. Trading style     → Trend / Mean reversion / Defensive
 *   Q4. Experience        → New / Some / Experienced
 *
 * Recommendations are scored by style + risk + tags so the top 3-5
 * matches from `STRATEGY_IDEAS` are surfaced.
 *
 * The component is self-contained: it can be launched from a button
 * anywhere, or used as a registration-flow step. It does NOT mutate
 * global state; results are written to localStorage so the dashboard
 * can show "Your style: Swing / Medium / Trend" as a chip.
 */
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, ChevronLeft, ChevronRight, Sparkles, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { STRATEGY_IDEAS, type StrategyIdea } from "@/lib/strategyIdeas";

type TimeHorizon = "scalping" | "day" | "swing" | "position";
type RiskTolerance = "low" | "medium" | "high";
type Style = "trend" | "reversion" | "defensive" | "any";
type Experience = "new" | "some" | "experienced";

interface Answers {
    horizon: TimeHorizon | null;
    risk: RiskTolerance | null;
    style: Style | null;
    experience: Experience | null;
}

const HORIZON_OPTIONS: Array<{
    value: TimeHorizon;
    label: string;
    description: string;
}> = [
    {
        value: "scalping",
        label: "Minutes",
        description: "In and out within minutes. Many trades, tight stops.",
    },
    {
        value: "day",
        label: "Hours",
        description: "Close everything by end of day. Active but not frantic.",
    },
    {
        value: "swing",
        label: "Days–weeks",
        description: "Ride trends for a few days. Most popular style.",
    },
    {
        value: "position",
        label: "Weeks–months",
        description: "Long-term, low time-in-market. Patient capital.",
    },
];

const RISK_OPTIONS: Array<{
    value: RiskTolerance;
    label: string;
    description: string;
}> = [
    { value: "low", label: "Low", description: "Capital preservation first. Smaller, surer wins." },
    { value: "medium", label: "Medium", description: "Balanced risk/reward. Default for most." },
    { value: "high", label: "High", description: "Aggressive sizing. Accept drawdowns." },
];

const STYLE_OPTIONS: Array<{
    value: Style;
    label: string;
    description: string;
}> = [
    {
        value: "trend",
        label: "Follow trends",
        description: "Buy strength, sell weakness. Catch big moves.",
    },
    {
        value: "reversion",
        label: "Fade extremes",
        description: "Buy dips, sell rips. Mean reversion.",
    },
    {
        value: "defensive",
        label: "Defensive / yield",
        description: "Preserve capital, generate yield, hedge.",
    },
    { value: "any", label: "No preference", description: "Surprise me." },
];

const EXPERIENCE_OPTIONS: Array<{
    value: Experience;
    label: string;
    description: string;
}> = [
    { value: "new", label: "Brand new", description: "First time using an automated tool." },
    { value: "some", label: "Some experience", description: "Tried a few bots or strategies." },
    { value: "experienced", label: "Experienced", description: "I trade and write strategies already." },
];

const HORIZON_TO_IDEA_STYLES: Record<TimeHorizon, ReadonlyArray<string>> = {
    scalping: ["Scalping", "Day Trading"],
    day: ["Day Trading", "Scalping"],
    swing: ["Swing", "Trend"],
    position: ["Position", "Trend", "Defensive"],
};

function scoreIdea(idea: StrategyIdea, ans: Answers): number {
    if (!ans.horizon || !ans.risk || !ans.style) return 0;
    let score = 0;

    // Horizon match
    if (HORIZON_TO_IDEA_STYLES[ans.horizon].includes(idea.style)) score += 4;

    // Risk match
    const riskMatch: Record<RiskTolerance, number> = { low: 0, medium: 1, high: 2 };
    const dist = Math.abs(riskMatch[ans.risk] - riskMatch[idea.risk.toLowerCase() as RiskTolerance]);
    score += Math.max(0, 3 - dist * 2);

    // Style match
    if (ans.style === "any") score += 1;
    else if (ans.style === "trend" && (idea.style === "Trend" || idea.style === "Position" || idea.style === "Day Trading")) score += 3;
    else if (ans.style === "reversion" && (idea.style === "Mean Reversion" || idea.style === "Swing" || idea.style === "Day Trading")) score += 3;
    else if (ans.style === "defensive" && idea.style === "Defensive") score += 3;
    else if (ans.style === "defensive" && idea.style === "Position") score += 2;

    // Experience modifier: new users → low risk, simple strategies
    if (ans.experience === "new") {
        if (idea.risk === "Low") score += 3;
        if (idea.style === "Scalping") score -= 4;
    } else if (ans.experience === "some") {
        if (idea.risk === "Medium") score += 2;
    } else {
        if (idea.risk === "High") score += 1;
    }

    return score;
}

function recommend(ans: Answers): StrategyIdea[] {
    return [...STRATEGY_IDEAS]
        .map((idea) => ({ idea, score: scoreIdea(idea, ans) }))
        .filter((x) => x.score > 0)
        .sort((a, b) => b.score - a.score)
        .slice(0, 5)
        .map((x) => x.idea);
}

interface StrategyStyleWizardProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    onComplete?: (answers: Answers, recommendations: StrategyIdea[]) => void;
}

export const StrategyStyleWizard: React.FC<StrategyStyleWizardProps> = ({
    open,
    onOpenChange,
    onComplete,
}) => {
    const [step, setStep] = useState(0);
    const [answers, setAnswers] = useState<Answers>({
        horizon: null,
        risk: null,
        style: null,
        experience: null,
    });

    const questions = [
        { key: "horizon" as const, title: "How long do you want to hold a trade?", options: HORIZON_OPTIONS },
        { key: "risk" as const, title: "How much drawdown can you stomach?", options: RISK_OPTIONS },
        { key: "style" as const, title: "How do you like to find entries?", options: STYLE_OPTIONS },
        { key: "experience" as const, title: "What's your experience level?", options: EXPERIENCE_OPTIONS },
    ];

    const isLastStep = step === questions.length;
    const isFirstStep = step === 0;
    const currentKey = step < questions.length ? questions[step].key : null;
    const currentAnswer = currentKey ? answers[currentKey] : null;
    const canAdvance = isLastStep
        ? true
        : currentAnswer !== null;

    const recommendations = useMemo(() => recommend(answers), [answers]);

    const handleSelect = (val: string) => {
        if (!currentKey) return;
        setAnswers((prev) => ({ ...prev, [currentKey]: val }) as Answers);
    };

    const handleFinish = () => {
        try {
            localStorage.setItem("depthsight_wizard_answers", JSON.stringify(answers));
        } catch {
            /* localStorage may be unavailable in private mode */
        }
        onComplete?.(answers, recommendations);
        // Reset for next time
        setStep(0);
        setAnswers({ horizon: null, risk: null, style: null, experience: null });
    };

    const handleClose = () => {
        onOpenChange(false);
        setTimeout(() => {
            setStep(0);
            setAnswers({ horizon: null, risk: null, style: null, experience: null });
        }, 200);
    };

    return (
        <Dialog open={open} onOpenChange={handleClose}>
            <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
                <DialogHeader>
                    <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                            <Sparkles className="w-5 h-5 text-indigo-600" />
                            <DialogTitle>
                                {isLastStep
                                    ? "Your strategy matches"
                                    : "Find your trading style"}
                            </DialogTitle>
                        </div>
                        <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            onClick={handleClose}
                        >
                            <X className="w-4 h-4" />
                        </Button>
                    </div>
                    <DialogDescription>
                        {isLastStep
                            ? "We picked 3-5 strategies that fit your answers. Click any one to open it in the AI Co-pilot."
                            : `Question ${step + 1} of ${questions.length}`}
                    </DialogDescription>
                </DialogHeader>

                {!isLastStep && currentKey && (
                    <div className="space-y-4">
                        <h3 className="text-lg font-semibold text-foreground">
                            {questions[step].title}
                        </h3>
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                            {questions[step].options.map((opt) => {
                                const isSelected = currentAnswer === (opt as any).value;
                                return (
                                    <button
                                        key={(opt as any).value}
                                        onClick={() => handleSelect((opt as any).value)}
                                        className={cn(
                                            "text-left p-4 rounded-lg border transition-all",
                                            isSelected
                                                ? "border-indigo-500 bg-indigo-50 dark:bg-indigo-950/30"
                                                : "border-border hover:border-indigo-300 hover:bg-muted/30",
                                        )}
                                    >
                                        <div className="font-medium text-sm text-foreground">
                                            {(opt as any).label}
                                        </div>
                                        <div className="text-xs text-muted-foreground mt-1">
                                            {(opt as any).description}
                                        </div>
                                    </button>
                                );
                            })}
                        </div>
                    </div>
                )}

                {isLastStep && (
                    <ResultsStep
                        recommendations={recommendations}
                        onClose={handleClose}
                    />
                )}

                <DialogFooter className="flex items-center justify-between sm:justify-between">
                    <Button
                        variant="outline"
                        onClick={() => setStep((s) => Math.max(0, s - 1))}
                        disabled={isFirstStep}
                        className="gap-1"
                    >
                        <ChevronLeft className="w-4 h-4" />
                        Back
                    </Button>
                    <div className="flex items-center gap-2">
                        {!isLastStep && (
                            <div className="flex gap-1">
                                {questions.map((_, i) => (
                                    <div
                                        key={i}
                                        className={cn(
                                            "h-1.5 w-6 rounded-full transition-colors",
                                            i <= step ? "bg-indigo-600" : "bg-muted",
                                        )}
                                    />
                                ))}
                            </div>
                        )}
                        {isLastStep ? (
                            <Button onClick={handleFinish} className="gap-1">
                                Save & close
                                <ArrowRight className="w-4 h-4" />
                            </Button>
                        ) : (
                            <Button
                                onClick={() => setStep((s) => s + 1)}
                                disabled={!canAdvance}
                                className="gap-1"
                            >
                                Next
                                <ChevronRight className="w-4 h-4" />
                            </Button>
                        )}
                    </div>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
};

const ResultsStep: React.FC<{
    recommendations: StrategyIdea[];
    onClose: () => void;
}> = ({ recommendations, onClose }) => {
    const navigate = useNavigate();

    const openInEditor = (idea: StrategyIdea) => {
        try {
            navigator.clipboard.writeText(idea.prompt);
        } catch {
            /* clipboard unavailable */
        }
        onClose();
        navigate("/editor");
    };

    if (recommendations.length === 0) {
        return (
            <div className="text-center py-8">
                <p className="text-muted-foreground">
                    No strategies match those answers. Try the Hub to browse all 27 ideas.
                </p>
            </div>
        );
    }

    return (
        <div className="space-y-2">
            {recommendations.map((idea) => (
                <div
                    key={idea.id}
                    className="flex items-start gap-3 p-3 rounded-lg border border-border/40 hover:border-indigo-300 hover:bg-indigo-50/30 dark:hover:bg-indigo-950/20 transition-all"
                >
                    <div className="flex-1 min-w-0">
                        <div className="font-medium text-sm text-foreground">{idea.title}</div>
                        <div className="text-xs text-muted-foreground mt-0.5 line-clamp-1">
                            {idea.summary}
                        </div>
                        <div className="flex gap-1.5 mt-1.5">
                            <span className="text-[10px] text-muted-foreground">
                                {idea.style} · {idea.risk} risk · {idea.timeframe}
                            </span>
                        </div>
                    </div>
                    <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs shrink-0"
                        onClick={() => openInEditor(idea)}
                    >
                        <Sparkles className="w-3 h-3 mr-1" />
                        Use
                    </Button>
                </div>
            ))}
        </div>
    );
};
