// src/hooks/useGoogleLoginMutation.ts
//
// The Google sign-in request, shared by the login page and the
// route-independent redirect handler.
//
// It was extracted from src/pages/Login.tsx unchanged, so that the handler
// which runs after Google redirects back to the site root hands the
// credential to the SAME mutation the login page used.

import { useMutation } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useToast } from "@/components/ui/use-toast";
import { useAuth } from "@/context/AuthContext";

export const useGoogleLoginMutation = () => {
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
		onError: (_error: Error) => {
			toast({
				variant: "destructive",
				title: t("toastFailureTitle"),
				description: "Google Login Failed",
			});
		},
	});
};
