// src/components/shared/PageEmptyState.tsx
// Reusable empty state with icon + title + description + action buttons.
// Used across pages (Research, MLCore, EventLog) to replace plain-text
// "no data" messages with something that tells the user what to do next.

import { ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";

export interface PageEmptyStateAction {
	label: string;
	href: string;
	variant?: "default" | "outline" | "secondary";
	primary?: boolean;
}

export const PageEmptyState = ({
	icon: Icon,
	title,
	description,
	actions,
	variant = "default",
}: {
	icon: React.ComponentType<{ className?: string }>;
	title: string;
	description: string;
	actions?: PageEmptyStateAction[];
	variant?: "default" | "outline" | "secondary";
}) => {
	return (
		<div className="flex flex-col items-center justify-center text-center py-12 px-6 max-w-md mx-auto">
			<div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-primary/10 mb-4">
				<Icon className="w-8 h-8 text-primary" />
			</div>
			<h3 className="text-lg font-semibold mb-1.5">{title}</h3>
			<p className="text-sm text-muted-foreground mb-5">{description}</p>
			{actions && actions.length > 0 && (
				<div className="flex flex-wrap gap-2 justify-center">
					{actions.map((a) => (
						<Button
							key={a.href}
							asChild
							variant={a.variant ?? (a.primary ? "default" : "outline")}
							size="sm"
						>
							<Link to={a.href}>
								{a.label}
								<ArrowRight className="w-3.5 h-3.5 ml-1.5" />
							</Link>
						</Button>
					))}
				</div>
			)}
			{!actions && variant === "outline" && (
				<Button asChild variant="outline" size="sm">
					<Link to="/hub">
						Browse templates
						<ArrowRight className="w-3.5 h-3.5 ml-1.5" />
					</Link>
				</Button>
			)}
		</div>
	);
};

export default PageEmptyState;
