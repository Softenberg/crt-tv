// CRT effect for mpv (heavy preset). Tweak the defines to taste.
//
//!HOOK OUTPUT
//!BIND HOOKED
//!DESC CRT (heavy)

#define CURVATURE   0.060  // barrel distortion amount
#define CORNER      0.035  // rounded corner radius
#define SCANLINES   0.50   // darkness of the gap between lines (0 = off)
#define SCAN_PERIOD 3.0    // screen pixels per scanline (480 / 3 = 160 lines)
#define MASK        0.20   // RGB aperture grille strength
#define SOFTNESS    0.45   // horizontal beam blur
#define CHROMA      1.2    // red/blue convergence error, in pixels
#define VIGNETTE    0.45
#define BRIGHTNESS  1.35   // compensates for scanline/mask darkening
#define NOISE       0.04

vec2 warp(vec2 uv) {
    vec2 c = uv * 2.0 - 1.0;
    c *= 1.0 + CURVATURE * vec2(c.y * c.y, c.x * c.x);
    return c * 0.5 + 0.5;
}

vec4 hook() {
    vec2 uv = warp(HOOKED_pos);
    if (any(lessThan(uv, vec2(0.0))) || any(greaterThan(uv, vec2(1.0))))
        return vec4(0.0, 0.0, 0.0, 1.0);

    // Beam: slight horizontal blur and red/blue misconvergence.
    vec2 px = vec2(HOOKED_pt.x, 0.0);
    vec3 col = vec3(HOOKED_tex(uv + px * CHROMA).r,
                    HOOKED_tex(uv).g,
                    HOOKED_tex(uv - px * CHROMA).b);
    vec3 blur = 0.5 * (HOOKED_tex(uv - px * 1.5).rgb + HOOKED_tex(uv + px * 1.5).rgb);
    col = mix(col, blur, SOFTNESS);
    float lum = dot(col, vec3(0.299, 0.587, 0.114));

    // Scanlines in screen space (avoids moire); bright pixels bleed into the gap.
    vec2 pix = floor(HOOKED_pos * HOOKED_size);
    if (mod(pix.y, SCAN_PERIOD) >= 1.0)
        col *= 1.0 - SCANLINES * (1.0 - 0.5 * lum);

    // Aperture grille.
    float m = mod(pix.x, 3.0);
    vec3 mask = vec3(1.0 - MASK);
    if (m < 1.0) mask.r = 1.0; else if (m < 2.0) mask.g = 1.0; else mask.b = 1.0;
    col *= mask;

    // Vignette and rounded corners.
    vec2 v = uv * (1.0 - uv.yx);
    col *= mix(1.0, clamp(pow(v.x * v.y * 15.0, 0.25), 0.0, 1.0), VIGNETTE);
    vec2 q = max(abs(uv - 0.5) - (0.5 - CORNER), 0.0);
    col *= 1.0 - smoothstep(CORNER - 0.004, CORNER, length(q));

    float n = fract(sin(dot(pix + random * 1000.0, vec2(12.9898, 78.233))) * 43758.5453);
    col += (n - 0.5) * NOISE;

    return vec4(clamp(col * BRIGHTNESS, 0.0, 1.0), 1.0);
}
