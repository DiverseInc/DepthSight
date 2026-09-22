// frontend/src/components/strategy-editor/AICoPilot.tsx

import { Loader2, WandSparkles } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

interface AICoPilotProps {
	onSubmit: (text: string) => void;
	isGenerating: boolean;
	className?: string;
}

export const AICoPilot: React.FC<AICoPilotProps> = ({
	onSubmit,
	isGenerating,
	className,
}) => {
	const { t } = useTranslation("strategy-editor");
	const [prompt, setPrompt] = useState("");

	const placeholderExamples = t("ai.placeholders", { returnObjects: true });
	const placeholder = Array.isArray(placeholderExamples)
		? placeholderExamples.join("\n")
		: "Describe your strategy here...";

	const handleSubmit = () => {
		if (prompt.trim() && !isGenerating) {
			onSubmit(prompt);
		}
	};

	// FIX 2026-09-22: the placeholders array was only used as gray placeholder
	// text inside the textarea — never clickable. New users had to copy-paste
	// or retype each example. Surface them as click-to-fill chips below the
	// textarea (only when prompt is empty, so they fade out as soon as the
	// user starts typing). Each chip sets the prompt to the full example,
	// which is a meaningful prompt the AI can act on.
	return (
		<Card className={cn("p-6 w-full", className)}>
			<div className="text-center mb-4">
				<WandSparkles className="w-10 h-10 text-primary mx-auto mb-3" />
				<h2 className="text-2xl font-bold mb-1">{t("ai.title")}</h2>
				<p className="text-muted-foreground">{t("ai.description")}</p>
			</div>
			<div className="relative">
				<Textarea
					value={prompt}
					onChange={(e) => setPrompt(e.target.value)}
					placeholder={placeholder}
					className="min-h-[100px] p-3 pr-36"
					disabled={isGenerating}
					onKeyDown={(e) => {
						if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
							handleSubmit();
						}
					}}
				/>
				<Button
					onClick={handleSubmit}
					disabled={isGenerating || !prompt.trim()}
					className="absolute bottom-3 right-3"
					size="sm"
				>
					{isGenerating ? (
						<Loader2 className="w-4 h-4 mr-2 animate-spin" />
					) : (
						<WandSparkles className="w-4 h-4 mr-2" />
					)}
					{t("ai.generateButton")}
				</Button>
			</div>
			{Array.isArray(placeholderExamples) && prompt.trim().length === 0 && (
				<div className="mt-3">
					<p className="text-xs text-muted-foreground mb-2 text-center">
						Or try one of these:
					</p>
					<div className="flex flex-wrap gap-2 justify-center">
						{placeholderExamples.map((example: string, i: number) => (
							<button
								key={i}
								type="button"
								onClick={() => setPrompt(example)}
								disabled={isGenerating}
								className="text-xs px-3 py-1.5 rounded-full border border-border/60 bg-card/40 hover:bg-card/80 hover:border-primary/40 text-muted-foreground hover:text-foreground transition-all text-left max-w-full disabled:opacity-50"
							>
								{example.length > 60
									? example.substring(0, 57) + "…"
									: example}
							</button>
						))}
					</div>
				</div>
			)}
			<p className="text-xs text-muted-foreground mt-3 text-center">
				{t("ai.shortcutHint")}
			</p>
			<p className="text-[10px] text-muted-foreground/50 mt-4 text-center leading-tight">
				{t("ai.disclaimer")}
			</p>
		</Card>
	);
};
