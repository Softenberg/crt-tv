// Analog sync glitch: tearing, jittering bands and a vertical roll.
// tv.py switches this on for a moment every now and then; it runs on the video
// before the CRT shader, so the torn picture still gets scanlines and curvature.
//
//!HOOK MAIN
//!BIND HOOKED
//!DESC CRT glitch

#define TEAR_SKEW   0.25   // how far the picture below the tear line slides
#define BAND_SHIFT  0.15   // max sideways jump of a jittering band
#define ROLL        0.15   // max vertical roll
#define CHROMA      0.004  // colour fringing

float hash(float n) {
    return fract(sin(n * 12.9898) * 43758.5453);
}

vec4 hook() {
    vec2 pos = HOOKED_pos;
    float f = float(frame);
    float slow = floor(f / 3.0);  // parts of the glitch hold for a few frames

    // Vertical hold slips: the picture rolls, with a dark sync bar at the seam.
    float roll = hash(slow + 1.7) < 0.5 ? hash(slow + 4.1) * ROLL : 0.0;
    vec2 uv = vec2(pos.x, fract(pos.y + roll));
    float bar = roll > 0.0 ? smoothstep(0.0, 0.025, abs(pos.y - (1.0 - roll))) : 1.0;

    // Random bands lose horizontal sync and jump sideways.
    float band = floor(pos.y * 30.0);
    float shift = 0.0;
    if (hash(band * 1.31 + slow * 7.7) > 0.7)
        shift = (hash(band * 3.17 + f) - 0.5) * BAND_SHIFT;

    // The tear: everything below a line skews sideways, with a little ripple.
    float tear = hash(slow * 2.3 + 0.5);
    if (pos.y > tear)
        shift += (pos.y - tear) * TEAR_SKEW * sign(hash(slow + 9.1) - 0.5)
               + 0.01 * sin(pos.y * 200.0 + f);
    uv.x += shift;

    float ca = CHROMA + abs(shift) * 0.2;
    vec3 c = vec3(HOOKED_tex(vec2(uv.x + ca, uv.y)).r,
                  HOOKED_tex(uv).g,
                  HOOKED_tex(vec2(uv.x - ca, uv.y)).b);
    if (uv.x < 0.0 || uv.x > 1.0)
        c = vec3(0.0);

    // Sparse bright noise lines.
    float line = hash(floor(pos.y * HOOKED_size.y) + f * 3.3);
    if (line > 0.97)
        c = mix(c, vec3(hash(line + f)), 0.6);

    return vec4(c * bar, 1.0);
}
