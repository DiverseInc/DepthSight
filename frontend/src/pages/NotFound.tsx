// src/pages/NotFound.tsx

import { ArrowLeft, Home, Search } from "lucide-react";
import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Logo } from "@/components/ui/logo";

const NotFound = () => {
	const { t } = useTranslation("common");
	const location = useLocation();
	const navigate = useNavigate();

	useEffect(() => {
		console.error(
			"404 Error: User attempted to access non-existent route:",
			location.pathname,
		);
	}, [location.pathname]);

	return (
		<div className="min-h-screen flex items-center justify-center bg-background relative overflow-hidden">
			{/* Brand radial backdrop, same as login */}
			<div
				aria-hidden
				className="pointer-events-none absolute inset-0 opacity-30"
				style={{
					background:
						"radial-gradient(circle at 30% 30%, rgba(0,212,255,0.18), transparent 45%), radial-gradient(circle at 70% 70%, rgba(0,102,255,0.18), transparent 50%)",
				}}
			/>

			<div className="relative w-full max-w-lg px-6 text-center space-y-8">
				<div className="flex justify-center">
					<Logo className="h-10 w-auto" />
				</div>

				<div className="space-y-3">
					<p className="font-mono text-xs uppercase tracking-widest text-primary">
						Error 404
					</p>
					<h1 className="text-5xl font-bold tracking-tight">
						{t("notFound.title") || "Page not found"}
					</h1>
					<p className="text-muted-foreground max-w-md mx-auto">
						{t("notFound.description") ||
							`We couldn't find anything at ${location.pathname}. The link may be broken or the page may have moved.`}
					</p>
					{location.pathname !== "/" && (
						<p className="font-mono text-xs text-muted-foreground/70 break-all">
							{location.pathname}
						</p>
					)}
				</div>

				<div className="flex flex-col sm:flex-row gap-2 justify-center">
					<Button asChild>
						<Link to="/">
							<Home className="mr-2 h-4 w-4" />
							{t("notFound.goHomeButton") || "Back to dashboard"}
						</Link>
					</Button>
					<Button variant="outline" onClick={() => navigate(-1)}>
						<ArrowLeft className="mr-2 h-4 w-4" />
						Go back
					</Button>
				</div>

				<div className="pt-6 border-t border-border/40">
					<p className="text-xs text-muted-foreground mb-3">
						Looking for something specific?
					</p>
					<div className="flex flex-wrap gap-2 justify-center text-xs">
						<Link
							to="/strategies"
							className="px-3 py-1.5 rounded-full border border-border/60 hover:border-primary/60 hover:text-primary transition-colors"
						>
							Strategies
						</Link>
						<Link
							to="/positions"
							className="px-3 py-1.5 rounded-full border border-border/60 hover:border-primary/60 hover:text-primary transition-colors"
						>
							Positions
						</Link>
						<Link
							to="/research"
							className="px-3 py-1.5 rounded-full border border-border/60 hover:border-primary/60 hover:text-primary transition-colors"
						>
							Research
						</Link>
						<Link
							to="/account"
							className="px-3 py-1.5 rounded-full border border-border/60 hover:border-primary/60 hover:text-primary transition-colors"
						>
							Account
						</Link>
					</div>
				</div>
			</div>
		</div>
	);
};

export default NotFound;
