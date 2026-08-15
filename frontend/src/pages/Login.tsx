// src/pages/Login.tsx

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { Eye, EyeOff, Loader2, Sparkles } from "lucide-react";
import { useState } from "react";
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
import { GoogleLogin } from "@react-oauth/google";

// Validation schema for the login form
const loginSchema = (t: (key: string) => string) =>
	z.object({
		username: z.string().min(1, t("usernameRequired")),
		password: z.string().min(1, t("passwordRequired")),
	});

type LoginFormValues = z.infer<ReturnType<typeof loginSchema>>;

// Demo credentials surfaced for the investor preview.
// These map to the rows created by scripts/seed_demo_data.py.
const DEMO_USERNAME = "alex_trader";
const DEMO_PASSWORD = "DemoPassword123!";

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


const useGoogleLoginMutation = () => {
	const { login } = useAuth();
	const { toast } = useToast();
	const { t } = useTranslation("login");

	return useMutation({
		mutationFn: async (googleToken: string) => {
			const response = await fetch("/api/v1/auth/google", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ token: googleToken }),
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
				description: "Google Login Failed",
			});
		},
	});
};


export default function LoginPage() {
	const { t } = useTranslation(["login", "common"]);
	const { toast } = useToast();
	const [showPassword, setShowPassword] = useState(false);

	const form = useForm<LoginFormValues>({
		resolver: zodResolver(loginSchema(t)),
		defaultValues: { username: "", password: "" },
	});
	const loginMutation = useLoginMutation();
	const googleLoginMutation = useGoogleLoginMutation();

	const onSubmit = (data: LoginFormValues) => {
		const formData = new URLSearchParams();
		formData.append("username", data.username);
		formData.append("password", data.password);
		loginMutation.mutate(formData);
	};

	const fillDemoCredentials = () => {
		form.setValue("username", DEMO_USERNAME);
		form.setValue("password", DEMO_PASSWORD);
		toast({
			title: "Demo credentials loaded",
			description: "Click Sign in to continue as alex_trader.",
		});
	};

	return (
		<div className="fixed inset-0 flex items-center justify-center bg-background overflow-hidden z-50">
			{/* Subtle radial backdrop using the brand cyan */}
			<div
				aria-hidden
				className="pointer-events-none absolute inset-0 opacity-30"
				style={{
					background:
						"radial-gradient(circle at 20% 20%, rgba(0,212,255,0.18), transparent 45%), radial-gradient(circle at 80% 70%, rgba(0,102,255,0.18), transparent 50%)",
				}}
			/>

			<div className="relative w-full max-w-md px-4 space-y-6">
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
							<GoogleLogin
								onSuccess={(credentialResponse) => {
									if (credentialResponse.credential) {
										googleLoginMutation.mutate(credentialResponse.credential);
									}
								}}
								onError={() => {
									toast({
										variant: "destructive",
										title: t("toastFailureTitle"),
										description: "Google Login Failed",
									});
								}}
							/>
						</div>

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

				{/* Demo helper — surfaces test login for the investor preview */}
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
										<span>{DEMO_USERNAME}</span>
									</div>
									<div className="flex justify-between gap-2">
										<span className="text-muted-foreground">pass</span>
										<span>{DEMO_PASSWORD}</span>
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

				<p className="text-center text-xs text-muted-foreground">
					By signing in you agree to our Terms and Privacy Policy.
				</p>
			</div>
		</div>
	);
}
