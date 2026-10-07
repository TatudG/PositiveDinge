/* PositiveDinge — Themenfilter.

   Bewusst klein gehalten und reine Zugabe: ohne JavaScript sind alle Karten
   sichtbar und die Seite funktioniert vollstaendig. Hier wird nur ausgeblendet,
   was gerade nicht interessiert. Nichts wird nachgeladen, nichts gemessen,
   nichts gesendet. */

(function () {
  'use strict';

  var chips = document.getElementById('chips');
  var strom = document.getElementById('strom');
  var leer = document.getElementById('leer');

  if (!chips || !strom) {
    return;
  }

  var karten = Array.prototype.slice.call(strom.querySelectorAll('.karte'));
  if (!karten.length) {
    return;
  }

  var gruppen = Array.prototype.slice.call(strom.querySelectorAll('.gruppe'));
  var knoepfe = Array.prototype.slice.call(chips.querySelectorAll('.chip'));

  function setzeFilter(wert) {
    var sichtbar = 0;

    karten.forEach(function (karte) {
      var passt = wert === 'alle' || karte.getAttribute('data-thema') === wert;
      karte.hidden = !passt;
      if (passt) {
        sichtbar++;
      }
    });

    // Datumsueberschriften mitverstecken, wenn in ihrer Gruppe nichts uebrig ist.
    gruppen.forEach(function (gruppe) {
      var offen = gruppe.querySelectorAll('.karte:not([hidden])').length;
      gruppe.hidden = offen === 0;
    });

    knoepfe.forEach(function (knopf) {
      var aktiv = knopf.getAttribute('data-filter') === wert;
      knopf.classList.toggle('is-aktiv', aktiv);
      knopf.setAttribute('aria-pressed', aktiv ? 'true' : 'false');
    });

    if (leer) {
      leer.hidden = sichtbar > 0;
    }
  }

  chips.addEventListener('click', function (ereignis) {
    var knopf = ereignis.target.closest('.chip');
    if (knopf) {
      setzeFilter(knopf.getAttribute('data-filter'));
    }
  });

  if (leer) {
    leer.addEventListener('click', function (ereignis) {
      if (ereignis.target.closest('.leer__zurueck')) {
        setzeFilter('alle');
      }
    });
  }
})();
