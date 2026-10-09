import 'dart:math' as math;

import 'package:flutter/foundation.dart';

/// Quota journalier de l'utilisateur (reset à minuit UTC).
///
/// Une limite à `null` signifie « illimité » (palier Pro).
/// Sources possibles :
///  * [UserQuota.fromJson] : backend FastAPI (`/me/quota`, événements SSE
///    `meta` / `done`, corps d'une réponse 429) — valeur temps réel (Redis) ;
///  * [UserQuota.fromRpc]  : RPC Supabase `get_my_quota` — peut avoir un léger
///    retard sur Redis, utile hors ligne du backend.
@immutable
class UserQuota {
  const UserQuota({
    required this.plan,
    required this.messagesUsed,
    required this.messagesLimit,
    required this.tokensUsed,
    required this.tokensLimit,
    required this.resetsIn,
    required this.fetchedAt,
  });

  final String plan;
  final int messagesUsed;
  final int? messagesLimit;
  final int tokensUsed;
  final int? tokensLimit;

  /// Délai avant la remise à zéro, mesuré à [fetchedAt].
  final Duration resetsIn;
  final DateTime fetchedAt;

  factory UserQuota.fromJson(Map<String, dynamic> json) {
    return UserQuota(
      plan: json['plan'] as String? ?? 'free',
      messagesUsed: _asInt(json['messages_used']) ?? 0,
      messagesLimit: _asInt(json['messages_limit']),
      tokensUsed: _asInt(json['tokens_used']) ?? 0,
      tokensLimit: _asInt(json['tokens_limit']),
      resetsIn: Duration(seconds: _asInt(json['resets_in']) ?? _secondesAvantMinuitUtc()),
      fetchedAt: DateTime.now(),
    );
  }

  /// Ligne renvoyée par `supabase.rpc('get_my_quota')`.
  factory UserQuota.fromRpc(Map<String, dynamic> row) {
    return UserQuota(
      plan: row['plan'] as String? ?? 'free',
      messagesUsed: _asInt(row['messages_used']) ?? 0,
      messagesLimit: _asInt(row['daily_message_limit']),
      tokensUsed: _asInt(row['tokens_used']) ?? 0,
      tokensLimit: _asInt(row['daily_token_limit']),
      resetsIn: Duration(seconds: _secondesAvantMinuitUtc()),
      fetchedAt: DateTime.now(),
    );
  }

  bool get isPro => plan == 'pro';
  bool get isUnlimited => messagesLimit == null && tokensLimit == null;

  int? get messagesRemaining =>
      messagesLimit == null ? null : math.max(0, messagesLimit! - messagesUsed);

  int? get tokensRemaining =>
      tokensLimit == null ? null : math.max(0, tokensLimit! - tokensUsed);

  /// Part consommée (0 → 1), `null` si illimité.
  double? get messagesRatio => _ratio(messagesUsed, messagesLimit);
  double? get tokensRatio => _ratio(tokensUsed, tokensLimit);

  /// La contrainte la plus serrée des deux (0 si tout est illimité).
  double get usageRatio => math.max(messagesRatio ?? 0.0, tokensRatio ?? 0.0);

  /// Vrai quand les tokens sont plus proches de leur limite que les messages.
  bool get tokensAreBinding => (tokensRatio ?? 0.0) > (messagesRatio ?? 0.0);

  bool get isExhausted =>
      (messagesRemaining != null && messagesRemaining == 0) ||
      (tokensRemaining != null && tokensRemaining == 0);

  DateTime get resetsAt => fetchedAt.add(resetsIn);

  /// Temps restant avant le reset, recalculé à l'instant présent.
  Duration get timeUntilReset {
    final restant = resetsAt.difference(DateTime.now());
    return restant.isNegative ? Duration.zero : restant;
  }

  UserQuota copyWith({
    String? plan,
    int? messagesUsed,
    int? tokensUsed,
  }) {
    return UserQuota(
      plan: plan ?? this.plan,
      messagesUsed: messagesUsed ?? this.messagesUsed,
      messagesLimit: messagesLimit,
      tokensUsed: tokensUsed ?? this.tokensUsed,
      tokensLimit: tokensLimit,
      resetsIn: resetsIn,
      fetchedAt: fetchedAt,
    );
  }

  @override
  bool operator ==(Object other) =>
      other is UserQuota &&
      other.plan == plan &&
      other.messagesUsed == messagesUsed &&
      other.messagesLimit == messagesLimit &&
      other.tokensUsed == tokensUsed &&
      other.tokensLimit == tokensLimit;

  @override
  int get hashCode => Object.hash(plan, messagesUsed, messagesLimit, tokensUsed, tokensLimit);

  @override
  String toString() =>
      'UserQuota($plan, messages: $messagesUsed/${messagesLimit ?? '∞'}, '
      'tokens: $tokensUsed/${tokensLimit ?? '∞'})';

  static double? _ratio(int used, int? limit) {
    if (limit == null) return null;
    if (limit <= 0) return 1;
    return (used / limit).clamp(0.0, 1.0).toDouble();
  }

  static int? _asInt(Object? value) {
    if (value == null) return null;
    if (value is int) return value;
    if (value is num) return value.toInt();
    return int.tryParse(value.toString());
  }

  static int _secondesAvantMinuitUtc() {
    final maintenant = DateTime.now().toUtc();
    final minuit = DateTime.utc(maintenant.year, maintenant.month, maintenant.day + 1);
    return minuit.difference(maintenant).inSeconds;
  }
}
