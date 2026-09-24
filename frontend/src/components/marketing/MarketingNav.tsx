// src/components/marketing/MarketingNav.tsx

import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/context/AuthContext";
import { Logo } from "@/components/ui/logo";

export const MarketingNav = () => {
	const { isAuthenticated } = useAuth();

	return (
		<header className="sticky top-0 z-40 w-full border-b border-border/40 bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60">
			<div className="container flex h-16 max-w-screen-xl items-center justify-between px-4">
				<Link
					to={isAuthenticated ? "/dashboard" : "/welcome"}
					className="flex items-center gap-2"
				>
					<Logo className="h-7 w-auto" />
					<span
						className="text-lg font-bold tracking-tight"
						style={{ color: "var(--logo-text-color, hsl(var(--foreground)))" }}
					>
						DepthSight
					</span>
				</Link>
				<nav className="flex items-center gap-2">
					{isAuthenticated ? (
						<Button asChild>
							<Link to="/dashboard">Open dashboard</Link>
						</Button>
					) : (
						<>
							<Button asChild variant="ghost" size="sm">
								<Link to="/login">Sign in</Link>
							</Button>
							<Button asChild size="sm">
								<Link to="/register">Start free</Link>
							</Button>
						</>
					)}
				</nav>
			</div>
		</header>
	);
};
