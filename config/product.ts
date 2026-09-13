/** White-label identity. Replace these values once before launch. */
export const PRODUCT = {
  name: "Nexora",
  shortName: "N",
  company: "Your Company",
  supportEmail: "support@example.com",
  description: "Build, deploy, and govern multilingual voice and chat agents.",
} as const;

export const SUPPORTED_LOCALES = [
  { id: "ar", label: "العربية", direction: "rtl", speechLocale: "ar-AE" },
  { id: "en-US", label: "English (USA)", direction: "ltr", speechLocale: "en-US" },
  { id: "en-GB", label: "English (UK)", direction: "ltr", speechLocale: "en-GB" },
  { id: "hi-IN", label: "हिन्दी", direction: "ltr", speechLocale: "hi-IN" },
  { id: "hi-en", label: "Hinglish", direction: "ltr", speechLocale: "hi-IN" },
] as const;
