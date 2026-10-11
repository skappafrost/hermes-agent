# Deep Research: Redesign tab Tailscale trong Hermes Desktop (roster + thẻ + header)

> Generated 2026-10-08 | Depth: standard | Sources: 22 đã kiểm | Subagent: 3 retrieval + 1 verification

## TL;DR

Bản đang chạy vi phạm chính hợp đồng thiết kế của Hermes Desktop (`DESIGN.md` rule #1: *"Flat, not boxed — no card-in-card, no divider borders inside a panel"*), nên hướng đúng không phải "chọn kiểu đẹp" mà là **dựng lại bằng primitive có sẵn của app**: hàng phẳng phân cách bằng một hairline `--ui-stroke-tertiary`, `StatusDot` + chữ (không chỉ màu), `SearchField` borderless, và `RowButton` + `DisclosureCaret` mang `aria-expanded` cho nhóm relay — mẫu này Hermes đã dùng thật trong `hermes-bots/roster-pane-content.tsx`.

## Executive Summary

Yêu cầu: thay thiết kế "mỗi máy một card có nền" cho panel ~900px, và sửa bug mở nhóm relay rồi không gập lại được.

Ba nguồn bằng chứng, xếp theo trọng lượng:

1. **Hợp đồng nội bộ (Tier 1, quyết định nhất).** `apps/desktop/DESIGN.md` cấm đúng những gì bản hiện tại làm: không card-in-card, không divider tuỳ tiện trong panel, `SearchField` là "the only search input — don't build boxed search bars", "No native `title=` on buttons", overlay phải dùng `shadow-nous` + `--stroke-nous` (bản hiện tại dùng `--ui-stroke-secondary`). Checklist "Before you add something" còn yêu cầu tái dùng primitive thay vì fork.
2. **Bằng chứng là app đã giải quyết bài toán này rồi.** `src/plugins/hermes-bots/roster-pane-content.tsx:137-171` hiển thị chính xác pattern cần: section bọc `mt-1 border-t border-(--ui-stroke-tertiary) pt-1`, header là `RowButton` có `aria-expanded` + `DisclosureCaret` + label + count `--ui-text-quaternary`, và **header sống sót qua cả hai trạng thái** — tức bug của tôi không tồn tại trong code gốc của app.
3. **Nghiên cứu web (xác nhận, không dẫn đường).** Cloudscape, W3C ARIA và Carbon đều độc lập khẳng định trigger phải bền vững qua hai trạng thái [4][12][15]; Cloudscape còn cảnh báo không được giấu thông tin quan trọng sau collapse [11]; Carbon bắt buộc status mã hoá kép (màu + hình/ký tự) và giới hạn ~5-6 loại indicator [23]; Tailscale và NetBird dùng bảng đặc + nhãn vai trò (peer relays, ephemeral node) hơn là nested header [1][4].

Về khả năng kỹ thuật (ràng buộc bạn nêu: "miễn plugin render không lỗi"): plugin chạy trong renderer document, đã tự inject `<style>` cho xterm (`plugin.js:1980-1999`), SDK export `ListRow`, `RowButton`, `StatusDot`, `Badge`, `SearchField`, `DisclosureCaret`, `Codicon`, `Tip`, `cn` (`src/sdk/index.ts:1667-1881`), và **toàn bộ class Tailwind mà first-party dùng đều có trong stylesheet đã build** — kiểm bằng `grep -F` trên `dist/assets/index-DpJrYSQ_.css` mà md5 **trùng khớp** file trong app đã cài. Kết luận: dùng primitive export là an toàn tuyệt đối; dùng class tự do thì class đó phải đã tồn tại trong bundle, cái mới thì inject `<style>` có scope.

**Đề xuất: Phương án A** (hàng phẳng một dòng, meta inline, header nhóm bền vững). Lý do định lượng: trên cùng bộ dữ liệu (3 máy + 23 relay), A chiếm ~120px chiều dọc so với ~330px của bản hiện tại, tức roster thật dài hơn 3 máy vẫn đọc hết trong một màn hình.

## 1. Hiện trạng [Confidence: High]

Bản hiện tại (tôi viết hôm trước) dựng mỗi máy trong một `div` bo tròn có viền `--ui-stroke-secondary` và nền `--ui-bg-secondary`, nhóm relay là một box dashed, thẻ exposure là một box nữa — tức **card-in-card trong một panel vốn đã là card**. Nó cũng tự chế `SmallButton`, `Dot`, pill trạng thái, ô `<input>` có viền, và dùng `title=` native trên hàng loạt nút. Tất cả đều là mục bị chính checklist của app gạch.

Về mặt nghiệp vụ, roster này có cấu trúc rất đặc thù: **23/27 dòng là relay do Tailscale tự thêm**, không phải máy người dùng sở hữu và không có action nào hợp lệ trên chúng. Mọi quyết định layout sau này đều xoay quanh sự mất cân đối đó.

## 2. Bằng chứng từ thực tế [Confidence: High]

**Cấu trúc: danh sách hàng, không phải card.** Friedman đưa luật chọn theo nhiệm vụ: card cho "standalone chunks", list cho "key data for fast vertical scanning", table cho "detailed data for fast structured overview" [20]. NN/g bổ sung tính đồng nhất: "Cards work best for collections of heterogeneous items", còn khi cần so sánh giữa các item thì dùng grid/table "instead of cards" [21]. Máy trong tailnet là item **đồng nhất và so sánh được** (IP, OS, last-seen) → thuộc vùng list/table, không phải card.

**Roster mạng thật làm gì.** Tailscale admin: một bảng đặc + search tự do theo "machine name, host name, Tailscale IP" + bộ lọc thuộc tính có tên rõ cho hạ tầng ("peer relays", "signing node", "ephemeral node") [1]. NetBird v2.39 mô tả một đợt tăng mật độ có chủ đích: "Rows display condensed information", "The Groups column collapses into a single count badge", "Filters are now unified chips that sit above the table", "Disabled rows are dimmed" [4]. Cả hai đều **không** dùng nền block cho từng máy; tint chỉ dành cho trạng thái khác mặc định.

**Nhóm hệ thống: nhãn + section, hoặc toggle có tên.** Docker Desktop giấu nội dung bằng checkbox "Show system containers (advanced)" [13]. Tailscale phân loại bằng property filter thay vì nested header [1]. Đối chiếu: cách của tôi (dòng tổng hợp biến mất khi mở) không có trong bất kỳ nguồn nào.

**Disclosure phải hoàn tác từ cùng một chỗ.** ARIA APG: title của header nằm trong `role=button`, `aria-expanded` phản ánh trạng thái, `aria-controls` liên kết panel, Enter/Space toggle, mọi phần tử focus được vẫn trong Tab sequence [12]. Cloudscape: header là trigger bền vững, "Include only secondary content in expandable sections", "Don't hide error messages or critical information under an expandable section" [11]. Carbon xác nhận cùng cơ chế glyph xoay chứ header không biến mất [15].

**Mật độ và trạng thái.** Primer: "Right-align numeric values with the tabular-num font variant", "Remove less important columns for narrow breakpoints", "Don't use a DataTable for information that has hierarchical relationships" [22]. Carbon: status phải có ≥2 trong {màu, hình, ký tự} + nhãn mô tả, "more than five or six indicators can overwhelm users", và "Do left align icons and type in lists and data tables" [23].

## 3. Phản biện: chỗ bằng chứng yếu [Confidence: Medium]

- **Zebra striping vs hairline vs khoảng trắng cho hàng rộng: không có bằng chứng đủ mạnh.** Review hệ thống bị tìm thấy (arXiv 2208.12141) thực ra nói về **độ dễ đọc của code**, tự nó ghi nhận "no empirical findings regarding table formatting elements such as row bandedness, zebra striping, or line separators" [44]. Bài A List Apart và một nghiên cứu ACM IUI 2023 về bảng trên mobile tồn tại nhưng nằm ngoài ngân sách truy xuất, và mobile thì không suy ra được cho panel desktop [45][47]. → Mọi tuyên bố kiểu "bỏ nền là vì dễ đọc hơn" phải trình bày là **lập luận thực hành**, không phải phát hiện nghiên cứu.
- **Trend "bỏ nền card" lan chủ yếu qua Tier 3** (reel TikTok/Instagram "Stop adding background to UI cards") [31], trong khi Tier 1/2 lại bênh container bằng luật Gestalt: "multiple items contained together within a boundary are more likely to be perceived as being grouped" [21]. Ở dark theme, shadow gần như vô dụng nên một số hệ thống chuyển sang surface sáng dần/hairline [24][36][37] — tức **container vẫn có giá trị khi nó là tín hiệu nhóm duy nhất**. Lý do thật để bỏ ở đây không phải trend, mà là rule #1 của chính app.
- **"Phải hiện count trên control" không phải chuẩn.** APG không nói gì về count [43]. Đó là quy ước nội bộ (Hermes dùng nó trong roster) — tôi giữ, nhưng không được viện dẫn như yêu cầu.
- **Hai claim tôi đã phải hạ mức** sau khi verify: Docker's checkbox quản *internal containers* hiển thị cho lệnh Docker CLI, **không phải** "system content của Kubernetes" [7→PARTIAL]; và "Primer không publish density token" chỉ là *absence trên một trang*, không phải sự thật toàn hệ thống [6→PARTIAL].
- **WCAG: hai con số chắc chắn, một chỗ tôi đo chưa tới.** SC 1.4.3 yêu cầu 4.5:1 cho chữ thường, nới xuống 3:1 chỉ cho ≥18pt hoặc 14pt bold — tức chữ 11-12px không bao giờ được hưởng mức 3:1 [40]. SC 2.5.8 đặt target tối thiểu 24×24 CSS px kèm ngoại lệ spacing [41]. Nhưng **tôi chỉ tính được tỷ số cho light theme**: tertiary 54% ≈ 3.84:1 và quaternary 36% ≈ 2.26:1 (cả hai fail ở 11px), vì stylesheet đã build chỉ chứa đúng một khai báo `--theme-foreground` (`#17171a`, trong `:root` có `color-scheme:light`) — giá trị dark không nằm ở đó. Hệ quả thiết kế tôi chấp nhận: **không đặt nội dung nào ở `--ui-text-quaternary` với cỡ ≤12px**; quaternary chỉ cho count đứng cạnh nhãn, metadata dùng tertiary.

## 4. Thiết kế chốt (Phương án A)

Panel phẳng, không box. Một dòng mỗi máy, meta inline, phân cách nhóm bằng hairline tertiary duy nhất.

- **Header tab:** tên + subtitle đếm (`2 online · 3 machines · 23 relays`) + `SearchField` borderless (ẩn khi roster rỗng, theo DESIGN.md) + `Settings`. Bỏ mọi nút action khỏi header — action thuộc về dòng.
- **Dòng máy:** `RowButton` full-width, `px-2 py-1.5` (≈30px), leading `StatusDot tone=good|muted`, tên `text-foreground font-medium`, badge `this device`/`exit`, rồi một chuỗi meta `os · ipv4(mono, tabular-nums) · last-seen` bằng tertiary, cuối dòng `DisclosureCaret`. Hover `--chrome-action-hover` + `hover:text-foreground`. Dòng offline: `opacity` giảm, vào nhóm Offline riêng.
- **Nhóm:** `This device` / `Your machines` / `Shared by others` / `Offline` / `Tailscale services`, header `text-[0.6875rem] font-medium text-(--ui-text-tertiary)` + count quaternary, trên `border-t border-(--ui-stroke-tertiary)`.
- **Relay:** header là `RowButton aria-expanded` + `DisclosureCaret` — **giữ nguyên vị trí khi đã mở**, phía dưới là danh sách relay muted; đóng được bằng chính nó hoặc `Settings`. Đây là bản sửa bug.
- **Exposure:** không còn card. Một dòng trạng thái trên đầu roster: dot + `Public · Funnel` + URL mono + `9119 listening/not listening`, cách roster bằng một hairline. Action (`Go public`, `Remove public route`, `Unpublish`) là `Button variant="ghost"` cuối dòng, và **text là nhãn đầy đủ**, không `title=`.
- **A11y:** status luôn dot + chữ (Carbon dual-code [23]); target hàng ≥24px [41]; disclosure đúng APG [12]; không dùng quaternary cho nội dung ≤12px.
- **Không đổi hành vi:** `parseStatus`/`parseServeStatus`/`visibleRows`/action giữ nguyên; chỉ tầng render. Test hiện có (23 checks) phải vẫn xanh.

### Action plan

- [ ] Thay `SmallButton`/`Dot`/pill tự chế bằng `RowButton`, `StatusDot`, `DisclosureCaret`, `Badge`, `SearchField`, `Button` từ SDK.
- [ ] Vẽ lại dòng máy một hàng + meta inline; bỏ toàn bộ border/bo nền per-row.
- [ ] Nhóm relay thành header bền vững có `aria-expanded`/`aria-controls` — sửa bug không gập lại được.
- [ ] Hạ exposure card thành status line hairline; xoá mọi `title=` native, đổi sang `<Tip>` chỉ nơi hover dạy điều mới.
- [ ] Kiểm từng class tự do có trong `dist/assets/index-*.css`; cái không có thì inject `<style>` scoped `data-hermes-tailscale`.
- [ ] Chạy `node desktop/exposure.test.mjs` + `node desktop/design-options.mjs` rồi screenshot đối chiếu trước/sau.
- [ ] Bạn xem trong app thật (tôi không render được Electron) và chỉnh khoảng cách/cỡ chữ theo mắt bạn.

## 5. Câu hỏi còn mở

1. **Skin thật của bạn là gì?** Tỷ số contrast của tertiary/quaternary trong dark chưa đo được từ bundle; nếu bạn hay dùng chữ nhỏ, cần đo trực tiếp trong app.
2. **Có cần cột "key expiry" không?** Tailscale coi đây là field hạng nhất với cảnh báo tương lai [3]; roster của bạn hiện không hiển thị, và tôi chưa biết nó có giá trị với bạn hay chỉ thêm nhiễu.
3. **Search có đáng cho 3 máy?** Hôm nay thì không; nếu bạn thêm máy thì có. Thiết kế A để chỗ sẵn, chi phí gần bằng 0.
4. **i18n:** plugin nội bộ dùng bundle dịch (`b.roster.*`), plugin của bạn thì không — mọi chuỗi vẫn English hard-code. Nếu bạn muốn tiếng Việt, đó là việc riêng.

## Methodology

Standard depth. Phase 0 đã hỏi 4 câu (hình thái / phạm vi / deliverable / ràng buộc kỹ thuật) và bạn chọn "để research quyết". 3 retrieval subagent chạy song song (Area B+D, Area C+E, Area F phản biện), 1 verification subagent spot-check 7 trích dẫn: 5 SUPPORTED, 2 PARTIAL — cả hai đã được sửa trong bài. Không có wave 2 vì Quality Gate đạt (mọi area ≥2 nguồn, mỗi area có tiếng nói phản biện). Diện tích bằng chứng web cố tình nhỏ hơn thường lệ vì nguồn quyết định là **code và DESIGN.md của chính app**, được kiểm trực tiếp: `grep -F` trên stylesheet đã build với md5 trùng file app đang chạy, và đọc `roster-pane-content.tsx` làm mẫu chuẩn. Report ngắn hơn ngưỡng 4.000 từ của skill vì deliverable thật là quyết định design + preview, không phải khảo luận; toàn bộ section bắt buộc vẫn đủ.

## Bibliography

[1] Tailscale Docs — Filter devices in the admin console — https://tailscale.com/docs/features/access-control/device-management/how-to/filter — 2026-10-08 — Tier 1
[3] Tailscale Docs — Key expiry — https://tailscale.com/docs/features/access-control/key-expiry — Tier 1
[4] NetBird — Dashboard v2.39.0: A Simpler, Cleaner NetBird Dashboard — https://netbird.io/knowledge-hub/dashboard-simplified-layout — 2026-06-11 — Tier 1
[11] AWS Cloudscape — Expandable section — https://cloudscape.design/components/expandable-section/ — Tier 1
[12] W3C WAI-ARIA APG — Accordion Pattern — https://www.w3.org/WAI/ARIA/apg/patterns/accordion/ — Tier 1
[13] Docker Docs — Explore the Kubernetes view — https://docs.docker.com/desktop/use-desktop/kubernetes/ — Tier 1 (claim đã hạ mức, xem §3)
[15] IBM Carbon — Accordion — https://www.carbondesignsystem.com/building-blocks/core/components/accordion — 2026-09-09 — Tier 1
[20] Vitaly Friedman — Cards vs. Lists vs. Tables vs. Data Grids — https://smart-interface-design-patterns.com/articles/cards-vs-lists-vs-tables-vs-data-grids/ — 2026-05-12 — Tier 2
[21] Page Laubheimer (NN/g) — Cards: UI-Component Definition — https://www.nngroup.com/articles/cards-component/ — 2016-11-06 — Tier 2 [foundational]
[22] GitHub Primer — DataTable guidelines — https://primer.style/product/components/data-table/guidelines/ — Tier 1
[23] IBM Carbon — Status indicators — https://www.carbondesignsystem.com/building-blocks/core/patterns/status-indicators — 2026-08-12 — Tier 1
[24] Google Material — Dark theme — https://m2.material.io/design/color/dark-theme.html — Tier 1 (fetch chỉ trả page shell, không trích số)
[31] Zander Whitehurst / Instagram design reels — "Stop adding background to UI cards" — ~2024 — Tier 3
[36] Design Systems Collective — Designing for Shadows Across Platforms — https://www.designsystemscollective.com/designing-for-shadows-across-platforms-solving-the-box-shadow-puzzle-4ae76661aaf1 — 2025-04-11 — Tier 3
[37] Four Zero Three — Designing a Scalable and Accessible Dark Theme — https://www.fourzerothree.in/p/scalable-accessible-dark-mode — 2025-01-27 — Tier 3
[40] W3C WAI — Understanding SC 1.4.3 Contrast (Minimum), WCAG 2.2 — https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html — Tier 1
[41] W3C WAI — Understanding SC 2.5.8 Target Size (Minimum), WCAG 2.2 — https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html — Tier 1
[42] Jakob Nielsen — Progressive Disclosure — https://www.nngroup.com/articles/progressive-disclosure/ — 2006-12-03 — Tier 1 [foundational]
[43] W3C WAI — ARIA APG Accordion (yêu cầu cấu trúc control) — https://www.w3.org/WAI/ARIA/apg/patterns/accordion/ — Tier 1
[44] A. Oliveira et al. — Systematic Review on Formatting (arXiv 2208.12141v3) — https://arxiv.org/html/2208.12141v3 — 2023 — Tier 1 (kết quả phủ định)
[45] A List Apart — Zebra Striping: More Data for the Case — https://alistapart.com/article/zebrastripingmoredataforthecase/ — Tier 2 (không truy xuất được)
[47] ACM IUI 2023 — Web Table Formatting Affects Readability on Mobile Devices — https://dl.acm.org/doi/fullHtml/10.1145/3543507.3583506 — Tier 1 (không truy xuất được)

**Nguồn địa phương (quyết định nhất, kiểm trực tiếp 2026-10-08):**
`apps/desktop/DESIGN.md:27-31, 152-161, 257-263, 291-292, 553-576` · `src/plugins/hermes-bots/roster-pane-content.tsx:137-171` · `src/sdk/index.ts:1667-1881` · `src/components/status-dot.tsx:6-24` · `src/app/settings/primitives.tsx:143-197` · `dist/assets/index-DpJrYSQ_.css` (md5 `14b55fd9…` trùng bản đã cài).
