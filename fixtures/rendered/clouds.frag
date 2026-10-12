// Shadertoy-Konvention: nur mainImage, Uniforms kommen vom Server.
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p){
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1,0)), u.x),
               mix(hash(i + vec2(0,1)), hash(i + vec2(1,1)), u.x), u.y);
}
float fbm(vec2 p){
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 6; i++) { v += a * noise(p); p *= 2.03; a *= 0.5; }
    return v;
}
void mainImage(out vec4 fragColor, in vec2 fragCoord){
    vec2 uv = (fragCoord - 0.5 * iResolution.xy) / iResolution.y;
    float t = iTime * 0.12;
    vec2 q = uv * 2.4 + vec2(t, -t * 0.6);
    float f = fbm(q + fbm(q * 1.7 + t));
    vec3 deep = vec3(0.05, 0.07, 0.14);
    vec3 warm = vec3(0.95, 0.62, 0.31);
    vec3 col = mix(deep, warm, smoothstep(0.35, 0.85, f));
    col += 0.25 * pow(max(0.0, 1.0 - length(uv) * 1.3), 3.0);
    fragColor = vec4(pow(col, vec3(0.4545)), 1.0);
}
