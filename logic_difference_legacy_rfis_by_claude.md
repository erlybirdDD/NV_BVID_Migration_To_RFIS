# Logic Differences: Order Service (Legacy) vs Retail Fulfillment Service (RFIS)

## Standard & Scheduled Delivery Options — In-Depth Comparison

This document covers the full logic flow for **Standard** and **Scheduled** delivery options across both services, identifying parity and mismatches for stores that are given both delivery options.

**Key Files Analyzed:**

- **Order Service**: `services/order-service-consumer/src/main/kotlin/com/doordash/rpc/order/consumer/mapper/DeliveryAvailabilityMapper.kt`
- **Order Service Common**: `libraries/order-service-common/src/main/kotlin/com/doordash/rpc/order/common/utils/DeliveryOptionsUtil.kt`
- **RFIS Handler**: `src/main/kotlin/com/doordash/retail/fulfillment/handler/GetBatchNewVerticalsDeliveryOptionDataHandler.kt`
- **RFIS Standard**: `src/main/kotlin/com/doordash/retail/fulfillment/model/StandardDeliveryOption.kt`
- **RFIS Scheduled**: `src/main/kotlin/com/doordash/retail/fulfillment/model/ScheduledDeliveryOption.kt`
- **RFIS DeliveryWindowBuilder**: `src/main/kotlin/com/doordash/retail/fulfillment/util/DeliveryWindowBuilder.kt`
- **RFIS TimeHelper**: `src/main/kotlin/com/doordash/retail/fulfillment/util/TimeHelper.kt`
- **RFIS Constants**: `src/main/kotlin/com/doordash/retail/fulfillment/util/Constants.kt`

---

## 1. Available Days

### Order Service (`DeliveryAvailabilityMapper.mapScheduleAvailableDays`)

- Fetches schedule hours from `deliveryAvailabilityResponseV2.scheduleAvailableHours` (sourced from MDS Store Availability).
- The number of days to fetch is controlled by `windowConfig.scheduledNumberOfDaysNeeded` (typically **5** for checkout).
- Groups windows into an `availableDaysMap: Map<Date, List<TimeWindow>>` keyed by the localized date of each midpoint.

### RFIS (`GetBatchNewVerticalsDeliveryOptionDataHandler.fetchStoreAvailability`)

- Uses `CHECKOUT_DAYS_SAV = 5` for checkout, with an explicit parity comment:

  > *"For parity purposes, the constant CHECKOUT_DAYS_SAV needs to be consistent with windowConfig.scheduledNumberOfDaysNeeded in DeliveryAvailabilityV2.kt of order service repo."*

- Also supports `CHECKOUT_DAYS_LONG = 30` for stores with long-time scheduling.
- Supports a `scheduleDaysOverride` experiment that can override the number of days.

### Parity Assessment

Generally aligned at 5 days. However, RFIS has an additional `scheduleDaysOverride` experiment that can change this, while the order service relies solely on `windowConfig.scheduledNumberOfDaysNeeded`. If the experiment is active, RFIS may return a different number of days than the order service expects.

---

## 2. Schedule Windows Per Day (Interval)

### Order Service

```kotlin
// DeliveryAvailabilityMapper.kt ~line 3097-3107
val intervalInMinutes = if (deliveryAvailabilityResponseV2.isPickup) {
    windowConfig.pickupIntervalInMinutes
} else if (isNonGroceryNewVerticalsWindowSizeChangeEnabled) {
    60
} else windowConfig.deliveryIntervalInMinutes

val roundUpToNearestMinute = if (isNonGroceryNewVerticalsWindowSizeChangeEnabled) {
    30
} else windowConfig.roundUpToNearestMinute
```

- **Pickup**: `windowConfig.pickupIntervalInMinutes`
- **Non-grocery NV** (BVIDs 68, 562, 139, 141, 166, 167, 169): **60 min** interval hardcoded
- **Default**: `windowConfig.deliveryIntervalInMinutes` (typically 20 min)

### RFIS (`DeliveryWindowBuilder.getScheduledInterval`)

- Delegates to `deliveryWindowConfiguration.getWindowDurationByTime()` or `getWindowDurationByDistance()` for ULDD.
- The entity configuration is fetched from CRDB and determines the interval per business/store.
- The interval is **dynamically configured** per entity, not hardcoded.

### Mismatch

Order Service hardcodes 60 min for NV BVIDs; RFIS reads from entity configuration. If the entity config differs from 60 min, windows per day will differ. Also, the default interval source is different — OS uses `windowConfig` runtime config, RFIS uses CRDB entity configuration.

---

## 3. Standard Eligibility

### Order Service

```kotlin
// DeliveryAvailabilityMapper.kt ~line 3610-3611 / 3869
rfisDeliveryAvailabilityBuilder.asapAvailable =
    (standardOption.eligibility && !originalDeliveryAvailabilityBuilder.isKilled.value).toProtoBoolValue()
```

- Standard is available if: **RFIS says eligible** AND the store is **not killed**.
- The `isKilled` flag is an additional check from the order service's own data.

### RFIS (`StandardDeliveryOption`)

```kotlin
val standardEnabled = !deliveryWindowEntityConfiguration.isOptionDisabled(
    getDeliveryOptionType(), deliveryOptionInfo.currentTime, logger, loggerPrefix,
) && deliveryOptionInfo.isStoreOpen() && deliveryOptionInfo.searchStoreUnavailableReason.isNullOrEmpty()
```

- Standard is eligible if: **not disabled by entity config** AND **store is open** AND **no unavailable reason**.

### Parity Assessment

The order service augments RFIS eligibility with the `isKilled` check. If `isKilled = true` (store is killed on OS side) but RFIS says `eligibility = true`, the OS will override to `asapAvailable = false`. This is intentional — OS owns the "killed" concept.

---

## 4. Window Calculation & Midpoint Generation

This is where the **most significant divergences** exist.

### 4a. First Window Start Time

#### Order Service

```kotlin
// DeliveryAvailabilityMapper.kt ~line 3162-3178
val midpointTimestamp = if (isProjectMic) {
    if (shouldInitiateFromNextHour(...)) {
        standardWindowUpperBound!!
    } else if (currentTime.plus(Constants.ASAP_TIME_DURATION_HOURS, ChronoUnit.HOURS) > storeDayOperationalHour.start) {
        currentTime.plus(Constants.ASAP_TIME_DURATION_HOURS, ChronoUnit.HOURS)
    } else storeDayOperationalHour.start
} else if (shouldShiftMidpointForRx) {
    storeDayOperationalHour.start.plus((intervalInMinutes / 2).toLong(), ChronoUnit.MINUTES)
} else {
    storeDayOperationalHour.start
}
```

- For **MIC (Grocery)**: First midpoint = `currentTime + 2 hours` (`ASAP_TIME_DURATION_HOURS`) if that's after store open, else store open time.
- With `shouldInitiateFromNextHour`: Uses `standardWindowUpperBound` as the starting point.
- For **Rx**: Shifts midpoint by half-interval from store start.
- For **default**: Uses `storeDayOperationalHour.start` directly.

#### RFIS (`ScheduledDeliveryOption.buildDeliveryWindows`)

```kotlin
val asapStartTimeWithPadding = deliveryOptionInfo.etaInfo.expressTimeMaxRange?.let {
    asapStartTime.plusMinutes(it.toLong())
} ?: asapStartTime
val scheduleStartTimeWithPadding = scheduleStartTime.plusMinutes(SCHEDULE_START_TIME_PADDING_MINUTES) // +5 min
val combinedStartTime = maxOf(asapStartTimeWithPadding, scheduleStartTimeWithPadding)
```

- Takes the **max** of:
  - `asapStartTime + ETA upper bound` (the ASAP delivery end time)
  - `scheduleStartTime + 5 min padding`
- For MIC: additionally sets first window after standard window upper bound.

#### MISMATCH

- OS uses `currentTime + 2 hours` for MIC as the initial midpoint.
- RFIS uses `currentTime + expressTimeMaxRange` (actual ETA) plus 5-min padding.
- **Example**: If the ETA is 45 min, OS would start at `now + 120min` but RFIS at `now + 50min` — dramatically different first scheduled window!

### 4b. Rounding Logic (CRITICAL MISMATCH)

#### Order Service

```kotlin
// DeliveryAvailabilityMapper.kt ~line 3181-3182
var midpointTimestampRoundedUpPreTruncate =
    if (midpointTimestamp.atZone(ZoneId.of("UTC")).minute.rem(roundUpToNearestMinute) != 0) {
        midpointTimestamp.plus(
            roundUpToNearestMinute - (midpointTimestamp.atZone(ZoneId.of("UTC")).minute.rem(roundUpToNearestMinute)).toLong(),
            ChronoUnit.MINUTES
        )
    } else midpointTimestamp
```

- Rounds using **UTC** timezone for the minute calculation.

#### RFIS (`TimeHelper.getRoundedUpTruncatedTime`)

```kotlin
fun getRoundedUpTruncatedTime(time: ZonedDateTime, roundUpMinutes: Int): ZonedDateTime {
    return if (time.minute.rem(roundUpMinutes) != 0) {
        time.plus(roundUpMinutes - time.minute.rem(roundUpMinutes).toLong(), MINUTES)
            .truncatedTo(MINUTES)
    } else {
        time.truncatedTo(MINUTES)
    }
}
```

- Rounds using the **store's local timezone** (the `ZonedDateTime` carries the timezone).

#### CRITICAL MISMATCH

For stores in timezones where the UTC offset has non-zero minutes (e.g., India UTC+5:30, parts of Australia UTC+9:30), the "minutes" part of the time differs between UTC and local, producing different rounding results. For US timezones (whole-hour offsets), this usually doesn't matter. But for **non-US markets**, this is a real discrepancy.

Additionally, RFIS **truncates** seconds/nanos after rounding (`truncatedTo(MINUTES)`), while OS does not explicitly truncate — though the `shouldTruncateWindowHours` flag can enable it conditionally.

### 4c. Today's First Window — Next-Hour Rounding

#### Order Service

```kotlin
// DeliveryAvailabilityMapper.kt ~line 3192-3209
var midpointTimestampRoundedUpAfterSanitization = if (shouldInitiateFromNextHour(
        eligibleForScheduleWindowFromNextHour,
        standardWindowUpperBound,
        storeDayOperationalHour
    )
) {
    val minsToAddedToReachNextHour = if (remainderMins != 0) {
        Constants.NUM_MINUTES_IN_HOUR - remainderMins
    } else {
        0
    }
    midpointTimestampRoundedUp
        .plus(minsToAddedToReachNextHour.toLong(), ChronoUnit.MINUTES)
        .plus((intervalInMinutes / 2).toLong(), ChronoUnit.MINUTES)
} else if (isNonGroceryNewVerticalsWindowSizeChangeEnabled && remainderMins == 0) {
    midpointTimestampRoundedUp
        .plus((intervalInMinutes / 2).toLong(), ChronoUnit.MINUTES)
} else midpointTimestampRoundedUp
```

- `shouldInitiateFromNextHour` flag + `eligibleForScheduleWindowFromNextHour` experiment.
- When enabled: Rounds up to the next full hour, then adds half-interval.
- For NV (60-min windows) with `remainderMins == 0`: Adds half-interval.

#### RFIS (`getRoundedUpMidpointTimestamp`)

```kotlin
// For Today:
return if (isToday) {
    getRoundedUpTruncatedTime(roundedUpMidpoint, Constants.SIXTY_MINUTE_ROUNDUP)
        .plus(intervalInMinutes.toLong() / 2, MINUTES)
} else if (isOneHourWindow) {
    roundedUpMidpoint.plus(intervalInMinutes.toLong() / 2, MINUTES)
} else if (isProjectUldd) {
    roundedUpMidpoint.plus(intervalInMinutes.toLong() / 2, MINUTES)
} else {
    roundedUpMidpoint
}
```

- For **Today**: **Always** rounds up to the nearest full hour, then adds half-interval.
- For non-Today with 1-hour windows already aligned: adds half-interval.

#### MISMATCH

OS requires the `eligibleForScheduleWindowFromNextHour` experiment to be enabled for next-hour rounding. RFIS always does this for Today. If the experiment is off in OS, the first scheduled window for Today could start at a different time.

---

## 5. First Display String

### Order Service

```kotlin
// DeliveryAvailabilityMapper.kt ~line 3221-3233
val displayString = if(isProjectMic) {
    DeliveryOptionsUtil.buildShortTimeDisplayString(
        midpointTimestampLocalized,
        intervalInMinutes,
        locale
    )
} else {
    DeliveryOptionsUtil.buildTimeDisplayStringFromMidpoint(
        midpointTimestampLocalized,
        intervalInMinutes,
        locale
    )
}
```

- For **MIC**: Uses `buildShortTimeDisplayString` — shows "8AM - 10AM" format (truncated hours when both are on the hour).
- For **non-MIC**: Uses `buildTimeDisplayStringFromMidpoint` — does `midpoint - halfInterval` to `midpoint + halfInterval` formatted with `DateTimeLocalizer.formatTime`.

The underlying `buildTimeDisplayString`:

```kotlin
fun buildTimeDisplayString(lowerBound: ZonedDateTime, upperBound: ZonedDateTime, locale: String): String {
    val localeObj = if (locale.isNotBlank()) Locales.toLocale(locale) else Locales.toLocale("en-US")
    val localizedLowerBound = DateTimeLocalizer.formatTime(lowerBound, localeObj, DateTimeFormat.SHORT)
    val localizedUpperBound = DateTimeLocalizer.formatTime(upperBound, localeObj, DateTimeFormat.SHORT)
    return String.format("$localizedLowerBound-$localizedUpperBound")
}
```

### RFIS (`DeliveryWindowBuilder`)

```kotlin
stringsForOption[TIME_WINDOW_CHECKOUT_DISPLAY_STRING] = StringContext(
    value = DeliveryWindowBuilder.buildTimeDisplayString(zonedStartTime, intervalInMinutes),
    requiresTranslation = false,
)
```

Underlying implementation:

```kotlin
fun buildTimeDisplayString(zonedStartTime: ZonedDateTime, zonedEndTime: ZonedDateTime): String {
    val localeObj = Locale.of(RequestContextUtils.locale())
    val localizedLowerBound = formatTime(zonedStartTime, localeObj)
    val localizedUpperBound = formatTime(zonedEndTime, localeObj)
    return String.format(localeObj, "$localizedLowerBound-$localizedUpperBound")
}
```

- When `enableNearestWindowString()`: Uses STS translation key `SCHEDULED_MIC_M1_TIME_WINDOW_FORMAT_KEY` with formatted lower/upper bounds.

### Mismatch in Approach

- OS builds the display string from the **midpoint** (`midpoint ± halfInterval`).
- RFIS builds from the **start time** (`start` to `start + interval`).
- Mathematically equivalent (`start = midpoint - half`, `end = midpoint + half`), but:
  - OS uses `Locales.toLocale(locale)` for formatting.
  - RFIS uses `Locale.of(RequestContextUtils.locale())` for formatting.
- If the locale resolution differs between the two, the formatted strings will differ.

---

## 6. Rounding Summary Table

| Aspect | Order Service | RFIS | Match? |
|--------|--------------|------|--------|
| Default round-up | 10 min | 10 min | ✅ Yes |
| NV BVIDs (68,562,139,141,166,167,169) | 30 min | 30 min | ✅ Yes |
| **Timezone for rounding** | **UTC** | **Store local TZ** | ❌ **NO** |
| **Truncation after rounding** | Conditional (`shouldTruncateWindowHours`) | Always (`truncatedTo(MINUTES)`) | ❌ **NO** |
| **Today first-hour rounding** | Gated by `eligibleForScheduleWindowFromNextHour` | Always for Today | ❌ **NO** |

---

## 7. The `compareDeliveryAvailabilityResults` Shadow Read

The function at line 508 of `DeliveryOptionsUtil.kt` performs a **full JSON-level diff** of the entire `DeliveryAvailability` protobuf:

```kotlin
val shadowResultMap = JSONObject(JSON_PRINTER.print(shadowResult)).toMap()
val originalResultMap = JSONObject(JSON_PRINTER.print(originalResult)).toMap()
val mismatches = Maps.difference(shadowResultMap, originalResultMap).entriesDiffering()
```

This means **every top-level field** in the `DeliveryAvailability` proto that differs will be caught. The `when` block handles specific known keys with custom tolerance logic, and a **catch-all `else` branch** flags any other differing field as `DeliveryOptionType.NOT_SET`.

### Explicitly Handled Fields & Tolerance

| Field | Comparison | Tolerance / Exceptions |
|-------|------------|----------------------|
| `deliveryOptions` (Express) | Sub-field diff of **first entry only** | Ignores `subTitle`, `title`, `optionQuoteMessage`, `isOptionSelectable` mismatches when high-demand |
| `asapAvailable` | Boolean match | Ignores when original=true, shadow=false, AND `isStoreOpenCurrently = false` (timing edge around store open/close) |
| `asapMinutesRange` | Integer array diff | Allows **±1 minute** difference per bound (timing drift); flags `_invalid_size` if array sizes ≠ 2, flags `_eta_deltas` if >1 min diff |
| `asapMinutesRangeString` | String match | Skipped entirely if `asapMinutesRange` already differs; ignores strings containing "mins" or "à" (known locale issues with no plan to fix) |
| `scheduledDeliveryAvailable` | Boolean match | Only flags when `isDeliveryAvailable = true` (ignores for unavailable stores) |
| `availableDays` | Count-level only | Flags `_number_of_days` if day counts differ; if same day count, only checks window counts per day and **ignores if exactly 1 day has a different window count** (known timing edge) |
| **Any other field** (catch-all `else`) | Existence diff | Always counted as valid mismatch, recorded as `DeliveryOptionType.NOT_SET` |

### Catch-All Fields (recorded via `else` branch)

Any top-level proto field not explicitly handled above that differs between shadow and original will be caught. This includes but is not limited to:

- `timezone`
- `isKilled`
- `isWithinDeliveryRegion` / `isOutsideDeliveryRegion`
- `nextScheduledDeliveryTime`
- `asapNumMinutesUntilClose` / `asapPickupNumMinutesUntilClose`
- `scheduleLongerInAdvanceTime`
- `scheduledDeliveryOptionQuoteMessage`
- `deliveryOptionsUiConfig`
- `asapPickupMinutesRange` / `asapPickupMinutesRangeString`
- `asapPickupAvailable`
- `asapDeliveryOverrideTitle` / `asapDeliveryOverrideSubtitle`
- `merchantShippingDayRangeString` / `isMerchantShippingAvailable` / `merchantShippingDayRange`
- `interruptions`

### Critical Blind Spots in the Comparison

**1. `deliveryOptions` only compares the FIRST entry (Express/Priority)**

```kotlin
val expressShadowResultMap = shadowResult.deliveryOptionsList.firstOrNull()?.let { ... }
val expressOriginalResultMap = originalResult.deliveryOptionsList.firstOrNull()?.let { ... }
```

Only `.firstOrNull()` is used — so if Standard, Schedule, FreeSameDay, Fast, Deferred, or any other delivery option exists at indices beyond 0, those entries are **never individually compared**. The parent-level diff detects that `deliveryOptions` as a whole differs, but the detailed sub-field breakdown (subTitle, title, etc.) only runs for the first element.

**2. Express sub-field comparison selectively ignores high-demand**

For the four fields `subTitle`, `title`, `optionQuoteMessage`, `isOptionSelectable`: if the shadow result contains the high-demand message (`"Due to high demand, currently unavailable"`), mismatches on these fields are **silently ignored**. All other express sub-fields (e.g., `etaMinutesRange`, `deliveryOptionType`, `isPreselected`, `description`, `subDescription`, `supplementalInfo`, `icon`, `footer`, `orderConfirmationDisplayString`) are always counted as mismatches regardless of high-demand state.

**3. `availableDays` only compares COUNTS — not actual window content**

The comparison checks:
- Are the **number of days** different?
- If same day count, are the **number of windows per day** different? (ignores if exactly 1 day has a window count diff)

It does **NOT** compare:
- Actual **window midpoint timestamps**
- **Display strings** of individual windows
- **rangeMin / rangeMax** timestamps of windows
- **intervalInMinutes** values
- **displayStringDeliveryWindow** (discount/savings strings)
- **dayTimestamp** values

**This means two results could have the same number of days and same number of windows per day, but completely different window start/end times or display strings, and the comparison would report 0 mismatches (success).**

**4. No comparison of `entriesOnlyOnLeft()` or `entriesOnlyOnRight()`**

The function only calls `Maps.difference(...).entriesDiffering()`, which catches fields that exist in **both** but have different values. It does **not** call `entriesOnlyOnLeft()` or `entriesOnlyOnRight()`, so:
- Fields that exist only in the shadow result (RFIS added a new field) are **not detected**.
- Fields that exist only in the original result (legacy has a field RFIS doesn't set) are **not detected**.

---

## 8. Complete Mismatch Summary

| # | Area | Order Service | RFIS | Impact |
|---|------|--------------|------|--------|
| 1 | **Rounding timezone** | UTC | Store local timezone | Different windows in non-whole-hour UTC offset timezones (India, parts of Australia) |
| 2 | **First schedule window start (MIC)** | `now + 2 hours` | `now + ETA max + 5 min` | Different first scheduled window — could be ~70 min apart |
| 3 | **Today hour rounding** | Gated by experiment | Always enabled | First window may differ when OS experiment is off |
| 4 | **Schedule interval source** | Runtime config / hardcoded 60 for NV | CRDB entity config | Windows-per-day may differ if config ≠ 60 |
| 5 | **Truncation after rounding** | Conditional | Always | Sub-minute precision diffs (usually <1 min) |
| 6 | **Schedule start padding** | None explicitly | 5-minute `SCHEDULE_START_TIME_PADDING_MINUTES` | RFIS pushes first window 5 min later |
| 7 | **Display string construction** | From midpoint ± half-interval | From start to start+interval | Equivalent math, but locale resolution may differ |
| 8 | **Standard start time (non-ASAP MIC)** | `currentTime + expressTimeMaxRange` | `currentTime + expressTimeMaxRange + 1 min` then rounds up | 1-min difference + rounding, amplified for some configs |
| 9 | **Schedule days override** | `windowConfig` only | `scheduleDaysOverride` experiment | RFIS can return different day counts |
| 10 | **Rx midpoint shift** | `start + halfInterval` (when `shouldShiftMidpointForRx`) | Combined start from ASAP + schedule padding | Different Rx schedule window alignment |

---

## 9. Recommendations

1. **Rounding timezone (#1)** is the most fundamental mismatch — migrating the OS to use the store timezone would align with RFIS.

2. **First schedule window start (#2)** is the highest-impact mismatch for grocery MIC stores — the 2-hour constant in OS vs the ETA-based logic in RFIS produces very different first windows.

3. **Today hour-rounding (#3)** can be resolved by fully rolling out the `eligibleForScheduleWindowFromNextHour` experiment.

4. For full parity, the OS window generation should be deprecated in favor of consuming RFIS-generated windows directly (as the `enableUnifiedDeliveryOptionMapping` / `fetchDeliveryAvailabilityWithOverrides` path already does for stores that have RFIS enabled).

5. **Schedule start padding (#6)** — consider adding the 5-minute padding in the OS path, or removing it from RFIS, to align the first window start time.

6. **Standard start time (#8)** — the `+1 min` difference in RFIS's `getStandardStartTime` should be accounted for in parity checks.

---

*Generated: March 10, 2026*
