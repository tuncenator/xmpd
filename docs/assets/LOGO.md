# xmpd logo

The approved geometric monogram and wordmark, generated and refined with the
built-in OpenAI image generation tool.

- [xmpd-logo-light.png](xmpd-logo-light.png): dark ink on a transparent background, for light themes.
- [xmpd-logo-dark.png](xmpd-logo-dark.png): white ink on a charcoal background, for dark themes.

The README selects the variant using a `picture` element. Keep the wordmark's
aspect ratio when displaying either image.

## Final refinement prompt

Use case: precise-object-edit / logo-brand
Edit the supplied approved xmpd logo, retaining its identity exactly. Preserve the four-piece geometric x emblem, the exact lowercase wordmark "xmpd", its existing glyph shapes, weight, and kerning. Make only these production refinements: reduce the emblem by approximately 10 percent relative to the wordmark, keep it optically vertically centered, and slightly increase the gap between emblem and wordmark. Use completely flat solid near-black ink (#17191e), clean smooth antialiased edges, and genuinely transparent alpha background. Remove any fill texture or shaded effect. Fit the finished horizontal lockup closely on a wide canvas with modest even padding around its visible bounds; do not retain the huge empty margins of the reference. One logo only, no captions, no new shapes, no mockup, no shadows, no gradients. This will appear at 400px wide in a GitHub README.

## Dark theme prompt

Use case: precise-object-edit / logo-brand
Create a dark-theme presentation of the supplied xmpd logo. Keep the exact four-piece geometric x and exact lowercase "xmpd" letterforms, proportions, placement and spacing from the reference. Change the black ink to clean solid white and place it on a COMPLETELY OPAQUE, SOLID DARK CHARCOAL background (#0d1117). This is a rectangular dark-background image, NOT a transparent cutout. All negative space, letter holes and gaps between emblem pieces must be the same solid charcoal as the background. Solid white flat logo silhouettes with crisp smooth edges. No texture, no distressed details, no contour outlines, no shading, no gradients. Preserve reference canvas aspect ratio and composition. Do not remove the background; the dark background is an essential part of this image.
