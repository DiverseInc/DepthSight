// src/pages/Login.tsx

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { Eye, EyeOff, Loader2, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";
import type { MouseEvent } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import * as z from "zod";
import { Logo } from "@/components/ui/logo";
import { Button } from "@/components/ui/button";
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from "@/components/ui/card";
import {
	Form,
	FormControl,
	FormField,
	FormItem,
	FormLabel,
	FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/use-toast";
import { useAuth } from "@/context/AuthContext";
import {
	areDemoCredentialsAvailable,
	DEMO_CREDENTIALS,
	isDemoCredentialsEnabled,
} from "@/lib/demoAccess";
import {
	createGoogleAuthorizeRequest,
	getGoogleClientId,
	rememberPendingGoogleRequest,
} from "@/lib/googleOAuth";

// Validation schema for the login form
const loginSchema = (t: (key: string) => string) =>
	z.object({
		username: z.string().min(1, t("usernameRequired")),
		password: z.string().min(1, t("passwordRequired")),
	});

type LoginFormValues = z.infer<ReturnType<typeof loginSchema>>;

// The seeded demo account, surfaced for the investor preview.
// It maps to the rows created by scripts/seed_demo_data.py.
//
// The credentials are NOT written here. They are injected at BUILD time by
// Vite's `define` (frontend/demo-build-flag.ts), which resolves to `null`
// unless the build asked for the demo helper. A production bundle therefore
// does not contain the username or the password at all -- previously they sat
// in module-level constants here and were readable in the shipped JavaScript
// even though the panel itself was gated off.

// Hook for the login API request
const useLoginMutation = () => {
	const { login } = useAuth();
	const { toast } = useToast();
	const { t } = useTranslation("login");

	return useMutation({
		mutationFn: async (data: URLSearchParams) => {
			const response = await fetch("/api/v1/token", {
				method: "POST",
				headers: { "Content-Type": "application/x-www-form-urlencoded" },
				body: data.toString(),
			});

			if (!response.ok) {
				const errorData = await response.json();
				throw new Error(errorData.detail || t("toastFailureTitle"));
			}
			return response.json();
		},
		onSuccess: async (data) => {
			await login(data);
			toast({ title: t("cardTitle"), description: t("toastSuccess") });
		},
		onError: (error: Error) => {
			toast({
				variant: "destructive",
				title: t("toastFailureTitle"),
				description: error.message,
			});
		},
	});
};


export default function LoginPage() {
	const { t } = useTranslation(["login", "common"]);
	const { toast } = useToast();
	const [showPassword, setShowPassword] = useState(false);
	// The demo helper is OFF unless the build asks for it (VITE_SHOW_DEMO).
	// Both conditions must hold: the build must have asked for it, and the
	// credentials must actually have been compiled in.
	const showDemoCredentials =
		isDemoCredentialsEnabled(
			import.meta.env as unknown as Record<string, unknown>,
		) && areDemoCredentialsAvailable;

	const form = useForm<LoginFormValues>({
		resolver: zodResolver(loginSchema(t)),
		defaultValues: { username: "", password: "" },
	});
	const loginMutation = useLoginMutation();

	// Google OAuth client id from Vite env. If not configured, we hide the
	// Google button entirely instead of rendering a broken OAuth flow.
	const googleClientId = getGoogleClientId();

	// Google sign-in, REDIRECT flow. The request is built ONCE per page
	// render so the `state` in the link and the `state` we remember on click
	// are the same value. See src/lib/googleOAuth.ts for why the redirect URI
	// is the bare origin and not /login.
	const googleSignIn = useMemo(
		() => createGoogleAuthorizeRequest(googleClientId),
		[googleClientId],
	);

	const startGoogleSignIn = (event: MouseEvent<HTMLAnchorElement>) => {
		// If the marker cannot be written, the sign-in CANNOT be verified when
		// it comes back -- so it must not start at all. Say so, loudly,
		// instead of sending the user to Google only to reject them on the
		// way back.
		if (!rememberPendingGoogleRequest(googleSignIn.state)) {
			event.preventDefault();
			toast({
				variant: "destructive",
				title: t("toastFailureTitle"),
				description: t("googleSignInStorageBlocked"),
			});
		}
	};

	const onSubmit = (data: LoginFormValues) => {
		const formData = new URLSearchParams();
		formData.append("username", data.username);
		formData.append("password", data.password);
		loginMutation.mutate(formData);
	};

	const fillDemoCredentials = () => {
		if (!DEMO_CREDENTIALS) return;
		form.setValue("username", DEMO_CREDENTIALS.username);
		form.setValue("password", DEMO_CREDENTIALS.password);
		toast({
			title: "Demo credentials loaded",
			description: `Click Sign in to continue as ${DEMO_CREDENTIALS.username}.`,
		});
	};

	return (
		<div className="fixed inset-0 overflow-y-auto bg-background z-50">
			{/* Subtle radial backdrop using the brand cyan */}
			<div
				aria-hidden
				className="pointer-events-none fixed inset-0 opacity-30"
				style={{
					background:
						"radial-gradient(circle at 20% 20%, rgba(0,212,255,0.18), transparent 45%), radial-gradient(circle at 80% 70%, rgba(0,102,255,0.18), transparent 50%)",
				}}
			/>

			<div className="relative w-full max-w-md mx-auto px-4 py-8 sm:py-12 space-y-6">
				{/* Brand header */}
				<div className="flex flex-col items-center text-center space-y-3">
					<Logo className="h-12 w-auto" />
					<div className="space-y-1">
						<h1 className="text-2xl font-semibold tracking-tight">
							{t("cardTitle") || "Welcome back"}
						</h1>
						<p className="text-sm text-muted-foreground">
							{t("cardDescription") ||
								"Sign in to your DepthSight trading workspace"}
						</p>
					</div>
				</div>

				<Card className="border-border/60 shadow-lg shadow-black/20">
					<CardContent className="pt-6">
						<Form {...form}>
							<form
								onSubmit={form.handleSubmit(onSubmit)}
								className="space-y-4"
							>
								<FormField
									control={form.control}
									name="username"
									render={({ field }) => (
										<FormItem>
											<FormLabel>{t("usernameLabel")}</FormLabel>
											<FormControl>
												<Input
													placeholder={t("usernamePlaceholder")}
													autoComplete="username"
													{...field}
												/>
											</FormControl>
											<FormMessage />
										</FormItem>
									)}
								/>
								<FormField
									control={form.control}
									name="password"
									render={({ field }) => (
										<FormItem>
											<div className="flex items-center justify-between">
												<FormLabel>{t("passwordLabel")}</FormLabel>
												<Link
													to="/forgot-password"
													className="text-xs text-muted-foreground hover:text-primary hover:underline"
												>
													{t("forgotPassword")}
												</Link>
											</div>
											<FormControl>
												<div className="relative">
													<Input
														type={showPassword ? "text" : "password"}
														placeholder={t("passwordPlaceholder")}
														autoComplete="current-password"
														{...field}
													/>
													<button
														type="button"
														onClick={() => setShowPassword((v) => !v)}
														className="absolute inset-y-0 right-0 flex items-center px-3 text-muted-foreground hover:text-foreground"
														aria-label={
															showPassword ? "Hide password" : "Show password"
														}
													>
														{showPassword ? (
															<EyeOff className="h-4 w-4" />
														) : (
															<Eye className="h-4 w-4" />
														)}
													</button>
												</div>
											</FormControl>
											<FormMessage />
										</FormItem>
									)}
								/>
								<Button
									type="submit"
									className="w-full"
									disabled={loginMutation.isPending}
								>
									{loginMutation.isPending ? (
										<>
											<Loader2 className="mr-2 h-4 w-4 animate-spin" />
											{t("button") || "Signing in…"}
										</>
									) : (
										t("button") || "Sign in"
									)}
								</Button>
							</form>
						</Form>

						{googleClientId && (
							<>
								<div className="relative my-5">
									<div className="absolute inset-0 flex items-center">
										<span className="w-full border-t" />
									</div>
									<div className="relative flex justify-center text-xs uppercase">
										<span className="bg-card px-2 text-muted-foreground">
											{t("common:or") || "Or"}
										</span>
									</div>
								</div>

								<div className="flex justify-center w-full my-2">
									<a
										href={googleSignIn.url}
										onClick={startGoogleSignIn}
										data-testid="google-signin"
										className="inline-flex h-10 w-full items-center justify-center gap-2 rounded-md border border-border/60 bg-background px-4 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
									>
										<svg
											aria-hidden="true"
											width="18"
											height="18"
											viewBox="0 0 48 48"
										>
											<path
												fill="#EA4335"
												d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
											/>
											<path
												fill="#4285F4"
												d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
											/>
											<path
												fill="#FBBC05"
												d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
											/>
											<path
												fill="#34A853"
												d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
											/>
										</svg>
										{t("googleSignIn") || "Sign in with Google"}
									</a>
								</div>
							</>
						)}

						<div className="mt-5 text-center text-sm text-muted-foreground">
							{t("noAccount")}{" "}
							<Link
								to="/register"
								className="font-medium text-primary hover:underline"
							>
								{t("registerLink")}
							</Link>
						</div>
					</CardContent>
				</Card>

				{/* Demo helper — surfaces test login for the investor preview.
				    OFF by default: the login page is public, so this must only
				    appear in a build that explicitly asks for it
				    (VITE_SHOW_DEMO=true). */}
				{showDemoCredentials && (
					<Card className="border-dashed border-primary/30 bg-primary/5">
						<CardContent className="pt-5">
							<div className="flex items-start gap-3">
								<div className="mt-0.5 inline-flex items-center justify-center w-8 h-8 rounded-full bg-primary/15 shrink-0">
									<Sparkles className="h-4 w-4 text-primary" />
								</div>
								<div className="flex-1 space-y-2">
									<div>
										<p className="text-sm font-medium">
											Touring the platform?
										</p>
										<p className="text-xs text-muted-foreground">
											Use the seeded demo account — password is pre-filled.
										</p>
									</div>
									<div className="rounded-md bg-background/60 border border-border/60 px-3 py-2 font-mono text-xs">
										<div className="flex justify-between gap-2">
											<span className="text-muted-foreground">user</span>
											<span>{DEMO_CREDENTIALS?.username}</span>
										</div>
										<div className="flex justify-between gap-2">
											<span className="text-muted-foreground">pass</span>
											<span>{DEMO_CREDENTIALS?.password}</span>
										</div>
									</div>
									<Button
										type="button"
										variant="outline"
										size="sm"
										className="w-full"
										onClick={fillDemoCredentials}
									>
										<Sparkles className="mr-2 h-3.5 w-3.5" />
										Use demo credentials
									</Button>
								</div>
							</div>
						</CardContent>
					</Card>
				)}

				<p className="text-center text-xs text-muted-foreground">
					By signing in you agree to our Terms and Privacy Policy.
				</p>
			</div>
		</div>
	);
}
