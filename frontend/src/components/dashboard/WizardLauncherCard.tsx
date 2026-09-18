/**
 * WizardLauncherCard
 * ───────────────────
 * Dashboard card that launches the Strategy Style Wizard.
 * Always visible on the Dashboard — once completed, the CTA switches to
 * "Re-take quiz" so users can refresh their strategy recommendations.
 * The dismiss button clears for the session only (next page load brings
 * the card back) so users can't permanently lose access to the wizard.
 */
import { useEffect, useState } from "react";
import { Sparkles, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { StrategyStyleWizard } from "@/components/StrategyStyleWizard";

const STORAGE_KEY = "depthsight_wizard_answers";

export const WizardLauncherCard = () => {
    const [open, setOpen] = useState(false);
    const [completed, setCompleted] = useState(false);
    const [dismissed, setDismissed] = useState(false);

    useEffect(() => {
        setCompleted(!!localStorage.getItem(STORAGE_KEY));
    }, []);

    if (dismissed) return null;

    const isCompleted = completed;

    return (
        <>
            <div className="relative flex flex-col sm:flex-row items-start sm:items-center gap-4 p-5 rounded-2xl border border-indigo-500/30 bg-gradient-to-r from-indigo-500/10 via-purple-500/10 to-pink-500/10">
                <div className="h-12 w-12 rounded-xl bg-indigo-500/20 flex items-center justify-center shrink-0">
                    <Sparkles className="h-6 w-6 text-indigo-600 dark:text-indigo-400" />
                </div>
                <div className="flex-1 min-w-0">
                    <h3 className="font-semibold text-base text-foreground">
                        {isCompleted
                            ? "Refresh your strategy picks"
                            : "Find your trading style"}
                    </h3>
                    <p className="text-sm text-muted-foreground mt-0.5">
                        {isCompleted
                            ? "Re-take the quiz to see new recommendations matched to your style."
                            : "4 quick questions and we'll match you with 3-5 strategies from our library. Takes about 60 seconds."}
                    </p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                    <Button
                        onClick={() => setOpen(true)}
                        size="sm"
                        className="bg-indigo-600 hover:bg-indigo-700"
                    >
                        <Sparkles className="w-3.5 h-3.5 mr-1.5" />
                        {isCompleted ? "Re-take quiz" : "Start quiz"}
                    </Button>
                    <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onClick={() => setDismissed(true)}
                        aria-label="Dismiss"
                    >
                        <X className="w-4 h-4" />
                    </Button>
                </div>
            </div>
            <StrategyStyleWizard
                open={open}
                onOpenChange={setOpen}
                onComplete={() => setCompleted(true)}
            />
        </>
    );
};
