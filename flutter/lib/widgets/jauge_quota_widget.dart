import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../models/user_quota.dart';

/// Jauge discrète de la consommation du jour : un trait fin et un compteur.
///
/// Branchée sur `MoteurConversationIA.quota`, elle se met à jour toute seule
/// à chaque échange. Rien ne s'affiche tant que le quota est inconnu.
/// Le détail (messages, tokens, heure du reset) apparaît en infobulle
/// (survol sur desktop / web, appui long sur mobile).
///
/// ```dart
/// JaugeQuotaWidget(quota: moteur.quota)
/// ```
class JaugeQuotaWidget extends StatelessWidget {
  const JaugeQuotaWidget({
    super.key,
    required this.quota,
    this.largeur = 72,
    this.onTap,
  });

  final ValueListenable<UserQuota?> quota;
  final double largeur;

  /// Ex. ouvrir la page d'abonnement quand le quota est épuisé.
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<UserQuota?>(
      valueListenable: quota,
      builder: (context, valeur, _) {
        return AnimatedSwitcher(
          duration: const Duration(milliseconds: 250),
          child: valeur == null
              ? const SizedBox.shrink()
              : _Jauge(quota: valeur, largeur: largeur, onTap: onTap),
        );
      },
    );
  }
}

class _Jauge extends StatelessWidget {
  const _Jauge({required this.quota, required this.largeur, this.onTap});

  final UserQuota quota;
  final double largeur;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final couleurs = theme.colorScheme;
    final ratio = quota.usageRatio;

    final couleur = quota.isExhausted
        ? couleurs.error
        : ratio >= 0.8
            ? couleurs.tertiary
            : couleurs.onSurfaceVariant;

    final style = theme.textTheme.labelSmall?.copyWith(
      color: couleur,
      letterSpacing: 0.8,
      fontFeatures: const [FontFeature.tabularFigures()],
    );

    final contenu = quota.isUnlimited
        ? Text('ILLIMITÉ', style: style)
        : Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              _Trait(ratio: ratio, couleur: couleur, fond: couleurs.outlineVariant, largeur: largeur),
              const SizedBox(width: 8),
              Text(_libelleCourt(quota), style: style),
            ],
          );

    return Semantics(
      label: _libelleAccessible(quota),
      button: onTap != null,
      child: Tooltip(
        message: _detail(quota),
        triggerMode: TooltipTriggerMode.longPress,
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(6),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
            child: ExcludeSemantics(child: contenu),
          ),
        ),
      ),
    );
  }

  /// Le compteur affiché suit la contrainte la plus serrée.
  static String _libelleCourt(UserQuota q) {
    if (q.tokensAreBinding || q.messagesLimit == null) {
      return '${(q.usageRatio * 100).round()} %';
    }
    return '${q.messagesUsed}/${q.messagesLimit}';
  }

  static String _detail(UserQuota q) {
    final lignes = <String>[
      'Messages : ${q.messagesUsed}${q.messagesLimit == null ? '' : ' / ${q.messagesLimit}'}',
      'Tokens : ${_milliers(q.tokensUsed)}${q.tokensLimit == null ? '' : ' / ${_milliers(q.tokensLimit!)}'}',
      'Réinitialisation dans ${_duree(q.timeUntilReset)}',
    ];
    if (q.isExhausted) lignes.insert(0, 'Limite du jour atteinte');
    return lignes.join('\n');
  }

  static String _libelleAccessible(UserQuota q) {
    if (q.isUnlimited) return 'Quota illimité';
    final restants = q.messagesRemaining;
    final base = restants == null
        ? 'Quota utilisé à ${(q.usageRatio * 100).round()} %'
        : '$restants messages restants aujourd\'hui';
    return '$base. Réinitialisation dans ${_duree(q.timeUntilReset)}.';
  }

  static String _duree(Duration d) {
    final heures = d.inHours;
    final minutes = d.inMinutes.remainder(60);
    if (heures == 0) return '$minutes min';
    return '$heures h ${minutes.toString().padLeft(2, '0')}';
  }

  /// 50000 -> « 50 000 » (espace fine insécable).
  static String _milliers(int n) {
    final s = n.toString();
    final buffer = StringBuffer();
    for (var i = 0; i < s.length; i++) {
      if (i > 0 && (s.length - i) % 3 == 0) buffer.write(' ');
      buffer.write(s[i]);
    }
    return buffer.toString();
  }
}

class _Trait extends StatelessWidget {
  const _Trait({required this.ratio, required this.couleur, required this.fond, required this.largeur});

  final double ratio;
  final Color couleur;
  final Color fond;
  final double largeur;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: largeur,
      height: 2,
      child: DecoratedBox(
        decoration: BoxDecoration(color: fond, borderRadius: BorderRadius.circular(1)),
        child: Align(
          alignment: Alignment.centerLeft,
          child: TweenAnimationBuilder<double>(
            tween: Tween(end: ratio),
            duration: const Duration(milliseconds: 600),
            curve: Curves.easeOutCubic,
            builder: (context, valeur, _) => FractionallySizedBox(
              widthFactor: valeur.clamp(0.0, 1.0).toDouble(),
              heightFactor: 1,
              child: DecoratedBox(
                decoration: BoxDecoration(color: couleur, borderRadius: BorderRadius.circular(1)),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
